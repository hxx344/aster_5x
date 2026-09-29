"""Pure routing and sanitized status for the two Feishu destinations."""
import os

import monitor
from . import monitoring

CHANNELS = ("event", "scheduled")
KEYS = {channel: (f"FEISHU_{channel.upper()}_WEBHOOK_URL", f"FEISHU_{channel.upper()}_SIGN_SECRET")
        for channel in CHANNELS}


def routing_mode(environ=None):
    environ = os.environ if environ is None else environ
    return "split" if any(key in environ for keys in KEYS.values() for key in keys) else "legacy"


def credentials(channel, environ=None):
    environ = os.environ if environ is None else environ
    keys = KEYS[channel]
    source = "dedicated"
    if routing_mode(environ) == "legacy":
        keys, source = ("FEISHU_WEBHOOK_URL", "FEISHU_SIGN_SECRET"), "legacy"
    webhook, secret = (environ.get(key, "") for key in keys)
    return {"webhook": webhook, "secret": secret, "source": source if webhook else "none"}


def for_item(item):
    category, _ = monitoring.metadata(item)
    return "scheduled" if category == "hourly_summary" else "event"


def public_status(errors=None, environ=None):
    result = {}
    for channel in CHANNELS:
        selected = credentials(channel, environ)
        configured = bool(selected["webhook"])
        error = (errors or {}).get(channel)
        if configured:
            try:
                monitor.validate_feishu_webhook(selected["webhook"])
                monitor.validate_feishu_secret(selected["secret"])
            except monitor.MonitorError:
                configured, error = False, "飞书配置无效"
        result[channel] = {"configured": configured, "source": selected["source"], "error": error}
    return result
