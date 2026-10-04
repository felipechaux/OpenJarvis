"""``jarvis mobile`` — pair phones with the running server and manage them."""

from __future__ import annotations

import json
import sys
from datetime import datetime

import click
import httpx

from openjarvis.core.config import DEFAULT_CONFIG_PATH, load_config


def _base() -> str:
    return f"http://127.0.0.1:{load_config().server.port}/v1/mobile"


def _call(method: str, path: str) -> dict:
    try:
        resp = httpx.request(method, _base() + path, timeout=10)
    except httpx.ConnectError:
        click.echo("JARVIS server is not running (start the app or `jarvis serve`).")
        sys.exit(1)
    if resp.status_code == 409:
        click.echo(
            "Mobile companion is disabled. "
            "Run `jarvis mobile enable`, then restart JARVIS."
        )
        sys.exit(1)
    if resp.status_code >= 400:
        click.echo(f"Error {resp.status_code}: {resp.text[:200]}")
        sys.exit(1)
    return resp.json()


@click.group("mobile")
def mobile() -> None:
    """Pair the JARVIS mobile app with this machine."""


@mobile.command("enable")
@click.option(
    "--public-url", default="", help="Tailscale/tunnel URL the phone tries first."
)
@click.option(
    "--fcm", "fcm_path", default="", help="Firebase service account JSON for push."
)
@click.option("--apns-key", default="", help="APNs .p8 key for iOS push.")
@click.option("--apns-key-id", default="", help="APNs key id (10 characters).")
@click.option("--apns-team-id", default="", help="Apple Developer team id.")
def enable(
    public_url: str,
    fcm_path: str,
    apns_key: str,
    apns_key_id: str,
    apns_team_id: str,
) -> None:
    """Turn the mobile companion on in config.toml (restart to apply)."""
    import tomlkit

    path = DEFAULT_CONFIG_PATH
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    server = doc.setdefault("server", tomlkit.table())
    mobile_tbl = server.setdefault("mobile", tomlkit.table())
    mobile_tbl["enabled"] = True
    if public_url:
        mobile_tbl["public_url"] = public_url
    if fcm_path:
        mobile_tbl["fcm_service_account"] = fcm_path
    for key, value in (
        ("apns_key_path", apns_key),
        ("apns_key_id", apns_key_id),
        ("apns_team_id", apns_team_id),
    ):
        if value:
            mobile_tbl[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))
    click.echo(
        f"Mobile companion enabled in {path}. "
        "Restart JARVIS, then run `jarvis mobile pair`."
    )


@mobile.command("pair")
@click.option(
    "--json", "as_json", is_flag=True, help="Print the raw payload instead of a QR."
)
def pair(as_json: bool) -> None:
    """Show a one-time QR to scan from the app (valid 5 minutes)."""
    from openjarvis.mobile.pairing import print_payload_qr

    payload = _call("POST", "/pair/start")["payload"]
    if not payload["urls"]:
        click.echo(
            "Warning: no reachable address found; "
            "set --public-url with `jarvis mobile enable`."
        )
    if as_json or not print_payload_qr(payload):
        if not as_json:
            click.echo(
                "(install the `mobile` extra for a terminal QR: uv sync --extra mobile)"
            )
        click.echo(json.dumps(payload, indent=2))
    click.echo(
        "Scan it from JARVIS on your phone. Addresses: " + ", ".join(payload["urls"])
    )


@mobile.command("devices")
def devices() -> None:
    """List paired devices."""
    rows = _call("GET", "/devices")["devices"]
    if not rows:
        click.echo("No paired devices.")
        return
    for d in rows:
        seen = (
            datetime.fromtimestamp(d["last_seen"]).strftime("%Y-%m-%d %H:%M")
            if d["last_seen"]
            else "never"
        )
        push = d["push"].get("provider") or "no push"
        click.echo(
            f"{d['id']}  {d['name']} ({d['platform']})  last seen {seen}  {push}"
        )


@mobile.command("revoke")
@click.argument("device_id")
def revoke(device_id: str) -> None:
    """Unpair a device; its token stops working immediately."""
    _call("DELETE", f"/devices/{device_id}")
    click.echo(f"Device {device_id} revoked.")
