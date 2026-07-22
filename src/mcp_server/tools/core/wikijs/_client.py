"""gSage AI — Wiki.js GraphQL API client.

Provides a thin async wrapper over the Wiki.js GraphQL API.
Uses Bearer token authentication and exposes the page operations
needed by the wikijs_editor tool.

Authentication
--------------
Provide a valid API token generated in the Wiki.js admin area
(Administration > API Access > New API Key) via the constructor
arguments ``url`` and ``api_token``.  Configure these via the
tool config (``TOOL_WIKIJS_EDITOR__URL`` / ``TOOL_WIKIJS_EDITOR__API_TOKEN``
env vars or the GSageToolConfig DB row).

Usage
-----
::

    async with WikijsClient(url=..., api_token=...) as client:
        pages = await client.list_pages(limit=20)
        page = await client.get_page_by_path("gsage/overview", "en")
        await client.update_page(page["id"], content="# Updated")
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_GRAPHQL_PATH = "/graphql"


def _sanitise_variables(variables: dict) -> dict:
    """Return a copy of *variables* safe for logging (no secrets)."""
    safe = {}
    for k, v in variables.items():
        if k in ("api_token", "token", "password"):
            safe[k] = "***"
        else:
            safe[k] = v
    return safe


class WikijsError(Exception):
    """Raised when the Wiki.js API returns an error response.

    Attributes
    ----------
    status_code : int
        HTTP status code, or 0 for connection/parse errors.
    error_code : int
        Wiki.js GraphQL error code (e.g. 6003 = PageNotFound).
    slug : str
        Wiki.js error slug (e.g. "PageNotFound").
    message : str
        Human-readable error description.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_code: int = 0,
        slug: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.slug = slug


class WikijsClient:
    """Async Wiki.js GraphQL API client.

    Parameters
    ----------
    url :
        Base URL of the Wiki.js instance (e.g. ``http://wikijs:3000``).
        Configure via ``TOOL_WIKIJS_EDITOR__URL`` or the tool's DB config row.
    api_token :
        Bearer token from Wiki.js API Access settings.
        Configure via ``TOOL_WIKIJS_EDITOR__API_TOKEN`` or the tool's DB config row.
    timeout :
        HTTP request timeout in seconds (default: 30).
    """

    def __init__(
        self,
        url: Optional[str] = None,
        api_token: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._url = (url or "").rstrip("/")
        self._api_token = api_token or ""
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None

    # ── Context manager ────────────────────────────────────────────────────

    async def __aenter__(self) -> "WikijsClient":
        if not self._url:
            raise WikijsError(
                "Wiki.js URL is not configured. Set 'url' in the tool config "
                "(TOOL_WIKIJS_EDITOR__URL or GSageToolConfig).",
                slug="CONFIG_MISSING",
            )
        if not self._api_token:
            raise WikijsError(
                "Wiki.js API token is not configured. Set 'api_token' in the tool config "
                "(TOOL_WIKIJS_EDITOR__API_TOKEN or GSageToolConfig).",
                slug="CONFIG_MISSING",
            )

        self._http = httpx.AsyncClient(
            base_url=self._url,
            timeout=self._timeout,
            follow_redirects=True,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_token}",
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Internal helpers ───────────────────────────────────────────────────

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("WikijsClient must be used as an async context manager.")
        return self._http

    async def _query(self, query: str, variables: Optional[dict] = None) -> dict[str, Any]:
        """Execute a GraphQL query/mutation and return the ``data`` payload.

        Raises
        ------
        WikijsError
            On HTTP errors, GraphQL errors, or unsuccessful responseResult.
        """
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        log.debug("Wiki.js GraphQL request:\n  query   = %s\n  variables = %s",
                   query.strip().replace("\n", "\n    ")[:500],
                   {k: v for k, v in (variables or {}).items() if k != "api_token"})

        http = self._get_http()
        try:
            resp = await http.post(_GRAPHQL_PATH, json=payload)
        except httpx.RequestError as exc:
            raise WikijsError(
                f"Failed to connect to Wiki.js at {self._url}: {exc}",
                slug="CONNECTION_ERROR",
            ) from exc

        if resp.status_code != 200:
            raise WikijsError(
                f"Wiki.js API returned HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
                slug="HTTP_ERROR",
            )

        body = resp.json()

        if "errors" in body:
            msgs = "; ".join(e.get("message", "unknown") for e in body["errors"])
            log.warning("Wiki.js GraphQL errors: %s", body["errors"])
            raise WikijsError(
                f"Wiki.js GraphQL error: {msgs}",
                status_code=resp.status_code,
                slug="GRAPHQL_ERROR",
            )

        return body.get("data", {})

    @staticmethod
    def _check_response_result(result: dict, operation: str, variables: Optional[dict] = None) -> None:
        """Raise WikijsError if responseResult.succeeded is False.

        Includes the GraphQL error details and (sanitised) variables
        in the exception message to aid debugging.
        """
        rr = result.get("responseResult") or {}
        if not rr.get("succeeded", True):
            vars_safe = _sanitise_variables(variables or {})
            detail = (
                f"Wiki.js {operation} failed: {rr.get('message', 'unknown error')} "
                f"(errorCode={rr.get('errorCode', 'N/A')}, slug={rr.get('slug', 'N/A')})"
            )
            if vars_safe:
                detail += f"\n  variables: {vars_safe}"
            raise WikijsError(
                detail,
                error_code=rr.get("errorCode", 0),
                slug=rr.get("slug", ""),
            )

    # ── Queries ────────────────────────────────────────────────────────────

    async def list_pages(self, limit: int = 50) -> list[dict]:
        """List all pages ordered by title (id, path, title, updatedAt).

        Parameters
        ----------
        limit :
            Maximum number of pages to return (default: 50).
        """
        gql = """
        query ListPages($limit: Int) {
          pages {
            list(limit: $limit, orderBy: TITLE) {
              id
              path
              title
              updatedAt
            }
          }
        }
        """
        data = await self._query(gql, {"limit": limit})
        return data.get("pages", {}).get("list", [])

    async def get_tree(self, path: str, locale: str) -> list[dict]:
        """Get the hierarchical page tree under a given path.

        Returns items with type PAGE or FOLDER.

        Parameters
        ----------
        path :
            Path prefix to list (e.g. ``"gsage"``).
        locale :
            Locale code (e.g. ``"en"``).
        """
        gql = """
        query GetTree($path: String, $locale: String!, $mode: PageTreeMode!) {
          pages {
            tree(path: $path, locale: $locale, mode: $mode) {
              id
              path
              title
              isFolder
              pageId
              parent
            }
          }
        }
        """
        data = await self._query(gql, {"path": path, "locale": locale, "mode": "ALL"})
        return data.get("pages", {}).get("tree", []) or []

    async def get_page(self, page_id: int) -> dict:
        """Fetch a single page by ID including full Markdown content.

        Parameters
        ----------
        page_id :
            Wiki.js page integer ID.
        """
        gql = """
        query GetPage($id: Int!) {
          pages {
            single(id: $id) {
              id
              path
              title
              description
              content
              updatedAt
              createdAt
              tags { tag }
            }
          }
        }
        """
        data = await self._query(gql, {"id": page_id})
        page = data.get("pages", {}).get("single")
        if not page:
            raise WikijsError(
                f"Page with ID {page_id} not found.",
                error_code=6003,
                slug="PageNotFound",
            )
        return page

    async def get_page_by_path(self, path: str, locale: str) -> dict:
        """Fetch a single page by path and locale including full Markdown content.

        Parameters
        ----------
        path :
            Page path (e.g. ``"gsage/overview"``).
        locale :
            Locale code (e.g. ``"en"``).
        """
        gql = """
        query GetPageByPath($path: String!, $locale: String!) {
          pages {
            singleByPath(path: $path, locale: $locale) {
              id
              path
              title
              description
              content
              updatedAt
              createdAt
              tags { tag }
            }
          }
        }
        """
        data = await self._query(gql, {"path": path, "locale": locale})
        page = data.get("pages", {}).get("singleByPath")
        if not page:
            raise WikijsError(
                f"Page at path '{path}' (locale: {locale}) not found.",
                error_code=6003,
                slug="PageNotFound",
            )
        return page

    async def search_pages(self, query: str, path_prefix: Optional[str] = None, locale: Optional[str] = None) -> list[dict]:
        """Full-text search across all pages.

        Parameters
        ----------
        query :
            Search query string.
        path_prefix :
            Optional path prefix to scope the search.
        locale :
            Optional locale code to filter results.
        """
        gql = """
        query SearchPages($query: String!, $path: String, $locale: String) {
          pages {
            search(query: $query, path: $path, locale: $locale) {
              results {
                id
                path
                title
                description
              }
            }
          }
        }
        """
        data = await self._query(gql, {"query": query, "path": path_prefix, "locale": locale})
        return data.get("pages", {}).get("search", {}).get("results", [])

    # ── Mutations ──────────────────────────────────────────────────────────

    async def update_page(
        self,
        page_id: int,
        content: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
        is_published: Optional[bool] = None,
    ) -> dict:
        """Update an existing page's content and optionally its metadata.

        Parameters
        ----------
        page_id :
            Wiki.js page integer ID to update.
        content :
            New Markdown content for the page.
        title :
            New title (optional — keeps existing if omitted).
        description :
            New description (optional).
        tags :
            New tags list (optional — pass ``[]`` to clear all tags,
            or the existing tag names to preserve them).
        is_published :
            Publication status (optional).
        """
        gql = """
        mutation UpdatePage(
          $id: Int!
          $content: String
          $title: String
          $description: String
          $tags: [String]
          $isPublished: Boolean
        ) {
          pages {
            update(
              id: $id
              content: $content
              title: $title
              description: $description
              tags: $tags
              isPublished: $isPublished
            ) {
              responseResult { succeeded errorCode slug message }
              page { id path title updatedAt }
            }
          }
        }
        """
        # Always include tags in the variables to prevent the Wiki.js resolver
        # from receiving ``undefined`` and crashing with
        # "Cannot read properties of undefined (reading 'map')".
        variables: dict[str, Any] = {
            "id": page_id,
            "content": content,
            "tags": tags or [],
        }
        if title is not None:
            variables["title"] = title
        if description is not None:
            variables["description"] = description
        if is_published is not None:
            variables["isPublished"] = is_published

        data = await self._query(gql, variables)
        result = data.get("pages", {}).get("update", {})
        self._check_response_result(result, "update", variables)
        return result.get("page", {})

    async def create_page(
        self,
        path: str,
        title: str,
        content: str,
        locale: str,
        description: str = "",
        tags: Optional[list[str]] = None,
        is_published: bool = True,
        is_private: bool = False,
    ) -> dict:
        """Create a new page.

        Parameters
        ----------
        path :
            Full page path (e.g. ``"gsage/new-page"``).
        title :
            Page title.
        content :
            Initial Markdown content.
        locale :
            Locale code (e.g. ``"en"``).
        description :
            Short description (optional).
        tags :
            List of tag strings (optional).
        is_published :
            Whether to publish immediately (default: True).
        is_private :
            Whether to mark as private (default: False).
        """
        gql = """
        mutation CreatePage(
          $path: String!
          $title: String!
          $content: String!
          $locale: String!
          $description: String!
          $editor: String!
          $isPublished: Boolean!
          $isPrivate: Boolean!
          $tags: [String]!
        ) {
          pages {
            create(
              path: $path
              title: $title
              content: $content
              locale: $locale
              description: $description
              editor: $editor
              isPublished: $isPublished
              isPrivate: $isPrivate
              tags: $tags
            ) {
              responseResult { succeeded errorCode slug message }
              page { id path title createdAt }
            }
          }
        }
        """
        data = await self._query(
            gql,
            {
                "path": path,
                "title": title,
                "content": content,
                "locale": locale,
                "description": description,
                "editor": "markdown",
                "isPublished": is_published,
                "isPrivate": is_private,
                "tags": tags or [],
            },
        )
        result = data.get("pages", {}).get("create", {})
        self._check_response_result(result, "create")
        return result.get("page", {})

    async def delete_page(self, page_id: int) -> None:
        """Permanently delete a page by its numeric ID.

        Parameters
        ----------
        page_id :
            Wiki.js page integer ID to delete.

        Raises
        ------
        WikijsError
            If the API reports failure or the page is not found.
        """
        gql = """
        mutation DeletePage($id: Int!) {
          pages {
            delete(id: $id) {
              responseResult { succeeded errorCode slug message }
            }
          }
        }
        """
        data = await self._query(gql, {"id": page_id})
        result = data.get("pages", {}).get("delete", {})
        self._check_response_result(result, "delete")
