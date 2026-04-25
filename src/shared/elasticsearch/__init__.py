"""Shared Elasticsearch package defining index templates, policies, and mappings.

This module defines all Elasticsearch indices for audit logs, metrics,
and application logs as specified in PHASE 2.
"""

from __future__ import annotations

from typing import Dict, Any

# Elasticsearch index prefix from settings
INDEX_PREFIX = "gsage-"

# Retention policies (days)
AUDIT_RETENTION_DAYS = 90
LOGS_RETENTION_DAYS = 30


def get_tool_audit_log_mapping() -> Dict[str, Any]:
    """Index mapping for tool execution audit trail.
    
    Index: tool_audit_log-YYYY-MM-DD
    Retention: 90 days
    """
    return {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "org_id": {"type": "keyword"},
                "user_id": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "tool_name": {"type": "keyword"},
                "tool_version": {"type": "keyword"},
                "input_params": {
                    "type": "object",
                    "enabled": True,
                },
                "output_data": {
                    "type": "object",
                    "enabled": False,  # stored but not indexed — saves Elasticsearch resources
                },
                "status": {"type": "keyword"},
                "error_code": {"type": "keyword"},
                "error_details": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                },
                "execution_time_ms": {"type": "integer"},
                "source": {"type": "keyword"},
            }
        },
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,  # Single-node deployment
            "index.lifecycle.name": f"{INDEX_PREFIX}audit-policy",
            "index.lifecycle.rollover_alias": f"{INDEX_PREFIX}tool-audit-log",
        },
    }


def get_llm_runs_mapping() -> Dict[str, Any]:
    """Index mapping for LLM call metrics.
    
    Index: llm_runs-YYYY-MM-DD
    Retention: 90 days
    """
    return {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "org_id": {"type": "keyword"},
                "user_id": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "conversation_id": {"type": "keyword"},
                "model": {"type": "keyword"},
                "role": {"type": "keyword"},  # maker or reviewer
                "input_tokens": {"type": "integer"},
                "output_tokens": {"type": "integer"},
                "total_tokens": {"type": "integer"},
                "latency_ms": {"type": "integer"},
                "status": {"type": "keyword"},
                "error_message": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
                },
            }
        },
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "index.lifecycle.name": f"{INDEX_PREFIX}audit-policy",
            "index.lifecycle.rollover_alias": f"{INDEX_PREFIX}llm-runs",
        },
    }


def get_agent_runs_mapping() -> Dict[str, Any]:
    """Index mapping for agent workflow metrics.
    
    Index: agent_runs-YYYY-MM-DD
    Retention: 90 days
    """
    return {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "org_id": {"type": "keyword"},
                "user_id": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "conversation_id": {"type": "keyword"},
                "input_hash": {"type": "keyword"},
                "agent_type": {"type": "keyword"},  # maker | reviewer | scheduled
                "status": {"type": "keyword"},
                "has_error": {"type": "boolean"},
                # Latency
                "total_duration_ms": {"type": "integer"},
                "elapsed_seconds": {"type": "float"},
                # Token usage
                "input_tokens": {"type": "integer"},
                "output_tokens": {"type": "integer"},
                "total_tokens": {"type": "integer"},
                # Tool invocations
                "tools_invoked": {"type": "keyword"},  # Array of tool names
                "tools_count": {"type": "integer"},
                # Content size (characters, not stored)
                "input_length": {"type": "integer"},
                "output_length": {"type": "integer"},
                # Multi-agent fields
                "review_cycles": {"type": "integer"},
                "maker_model": {"type": "keyword"},
                "reviewer_model": {"type": "keyword"},
                "source": {"type": "keyword"},
                "interface": {"type": "keyword"},
                "error_message": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
                },
            }
        },
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "index.lifecycle.name": f"{INDEX_PREFIX}audit-policy",
            "index.lifecycle.rollover_alias": f"{INDEX_PREFIX}agent-runs",
        },
    }


def get_app_logs_mapping() -> Dict[str, Any]:
    """Index mapping for structured application logs.
    
    Index: app_logs-YYYY-MM-DD
    Retention: 30 days
    """
    return {
        "mappings": {
            "properties": {
                "@timestamp": {"type": "date"},
                "level": {"type": "keyword"},
                "service": {"type": "keyword"},
                "org_id": {"type": "keyword"},
                "user_id": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "message": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
                },
                "context": {
                    "type": "object",
                    "enabled": True,
                },
                "error_stack": {
                    "type": "text",
                    "index": False,  # Don't index stack traces (storage only)
                },
            }
        },
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "index.lifecycle.name": f"{INDEX_PREFIX}logs-policy",
            "index.lifecycle.rollover_alias": f"{INDEX_PREFIX}app-logs",
        },
    }


def get_ilm_audit_policy() -> Dict[str, Any]:
    """Index Lifecycle Management policy for audit indices.
    
    Retention: 90 days, then delete.
    """
    return {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "rollover": {
                        "max_age": "1d",
                        "max_primary_shard_size": "10gb",
                    },
                    "set_priority": {"priority": 100},
                },
            },
            "delete": {
                "min_age": "90d",
                "actions": {"delete": {}},
            },
        }
    }


def get_ilm_logs_policy() -> Dict[str, Any]:
    """Index Lifecycle Management policy for application logs.
    
    Retention: 30 days, then delete.
    """
    return {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "rollover": {
                        "max_age": "1d",
                        "max_primary_shard_size": "5gb",
                    },
                    "set_priority": {"priority": 100},
                },
            },
            "delete": {
                "min_age": "30d",
                "actions": {"delete": {}},
            },
        }
    }


# Index template patterns
INDEX_TEMPLATES = {
    "tool_audit_log": {
        "index_patterns": [f"{INDEX_PREFIX}tool-audit-log-*"],
        "template": get_tool_audit_log_mapping(),
        "priority": 100,
    },
    "llm_runs": {
        "index_patterns": [f"{INDEX_PREFIX}llm-runs-*"],
        "template": get_llm_runs_mapping(),
        "priority": 100,
    },
    "agent_runs": {
        "index_patterns": [f"{INDEX_PREFIX}agent-runs-*"],
        "template": get_agent_runs_mapping(),
        "priority": 100,
    },
    "app_logs": {
        "index_patterns": [f"{INDEX_PREFIX}app-logs-*"],
        "template": get_app_logs_mapping(),
        "priority": 100,
    },
}

# ILM Policies
ILM_POLICIES = {
    f"{INDEX_PREFIX}audit-policy": get_ilm_audit_policy(),
    f"{INDEX_PREFIX}logs-policy": get_ilm_logs_policy(),
}
