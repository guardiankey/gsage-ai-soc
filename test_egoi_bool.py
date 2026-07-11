import sys
import egoi_api
from egoi_api import Configuration, schemas
from egoi_api.apis.tags.reports_api import ReportsApi
from egoi_api.exceptions import ApiTypeError, ApiValueError
import urllib3

def test():
    cfg = Configuration(host="https://api.egoiapp.com", api_key={"Apikey": "test"})
    client = egoi_api.ApiClient(cfg)
    api = ReportsApi(client)

    candidates = [
        ("Raw True", True),
        ("String 'true'", "true"),
        ("Integer 1", 1),
        ("schemas.BoolSchema(True)", schemas.BoolSchema(True)),
    ]

    if hasattr(schemas, "BoolClass") and hasattr(schemas.BoolClass, "TRUE"):
        candidates.append(("schemas.BoolClass.TRUE", schemas.BoolClass.TRUE))

    results = []

    for name, value in candidates:
        try:
            api.get_email_report(  # type: ignore[call-overload]
                query_params={"date": value},
                path_params={"campaign_hash": "deadbeef"},  # type: ignore[arg-type]
                skip_deserialization=True,
                timeout=3
            )
            results.append((name, "PASS", "Success (unexpectedly)"))
        except (ApiTypeError, ApiValueError) as e:
            err_msg = str(e)
            if "ref6570" in err_msg.lower() or "rfc6570" in err_msg.lower():
                results.append((name, "SERIALIZER_FAIL", err_msg))
            else:
                results.append((name, "VALIDATION_FAIL", f"{type(e).__name__}: {err_msg}"))
        except Exception as e:
            results.append((name, "PASS", f"Network error ({type(e).__name__})"))

    print("| Candidate | Result | Message |")
    print("|---|---|---|")
    for name, res, msg in results:
        msg = msg.replace("\n", " ").replace("|", "\\|")
        print(f"| {name} | {res} | {msg} |")

if __name__ == "__main__":
    test()
