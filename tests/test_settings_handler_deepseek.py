from lib.settings_handler import resolve_main_session_connection


def test_deepseek_gateway_hint_resolves_to_vendor_and_keeps_saved_env_mapping():
    resolved = resolve_main_session_connection(
        "https://api.deepseek.com/anthropic",
        values={
            "provider_id": "gateway",
            "env": {
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro[1m]",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            },
        },
        stored_provider_id="gateway",
    )

    assert resolved["provider_id"] == "deepseek"
    assert resolved["access_mode"] == "anthropic_native"
    assert resolved["provider_family"] == "deepseek"
    assert resolved["routing_models"] == {
        "opus": "deepseek-v4-pro[1m]",
        "sonnet": "deepseek-v4-pro[1m]",
        "haiku": "deepseek-v4-flash",
    }


def test_deepseek_preset_uses_current_v4_models():
    resolved = resolve_main_session_connection(
        "https://api.deepseek.com/anthropic",
        values={"provider_id": "deepseek"},
    )

    assert resolved["routing_models"] == {
        "opus": "deepseek-v4-pro",
        "sonnet": "deepseek-v4-pro",
        "haiku": "deepseek-v4-flash",
    }
