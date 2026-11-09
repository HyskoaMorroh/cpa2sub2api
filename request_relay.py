"""Optional config-bound request relay; direct imports do not require aiohttp."""
from relay.config import (RelayError, RELAY_METADATA, compile_bundle, export_bundle,
                          load_bundle, adapt_account, route_id, route_identity,
                          requires_relay, account_original_source, relay_model_mapping,
                          diagnostic_model_marker, diagnostic_model_alias,
                          diagnostic_model_mapping, is_diagnostic_model_marker)
from relay.protocol import authorized, prepare_request, restore_response, restore_sse_event


def verify_runtime(relay_base_url, bundle, timeout=5.0):
    from relay.server import verify_runtime as verify
    return verify(relay_base_url, bundle, timeout=timeout)


def main():
    from relay.server import main as serve_main
    return serve_main()


if __name__ == "__main__":
    raise SystemExit(main())
