import pytest

from crew.config.loader import dump_config, parse_config
from crew.config.schema import CrewConfig
from crew.domain.errors import ConfigError


def base(**overrides):
    data = {"version": 1, "project": {"name": "demo"}}
    for section, values in overrides.items():
        data[section] = values
    return data


def test_defaults_are_safe():
    config = parse_config(base())
    assert config.execution.max_implementers == 2
    assert config.approvals.require_plan_approval is True
    assert config.approvals.require_final_approval is True
    assert config.provider.claude_code.restricted is True


def test_roundtrip_through_yaml():
    import yaml

    config = parse_config(base())
    assert CrewConfig.model_validate(yaml.safe_load(dump_config(config))) == config


@pytest.mark.parametrize(
    "overrides",
    [
        {"execution": {"max_implementers": 3}},
        {"execution": {"max_implementers": 0}},
        {"approvals": {"require_plan_approval": False}},
        {"approvals": {"require_final_approval": False}},
        {"provider": {"claude_code": {"implementer_bash_rules": ["Bash(*)"]}}},
        {"provider": {"claude_code": {"implementer_bash_rules": ["rm -rf"]}}},
        {"git": {"worktree_directory": "/tmp/elsewhere"}},
        {"git": {"worktree_directory": ".crew/../outside"}},
        {"git": {"branch_prefix": "-x"}},
        {"unknown_section": {}},
    ],
)
def test_unsafe_or_invalid_settings_are_rejected(overrides):
    with pytest.raises(ConfigError):
        parse_config(base(**overrides))


def test_scoped_bash_rules_are_accepted():
    config = parse_config(
        base(provider={"claude_code": {"implementer_bash_rules": ["Bash(uv run pytest:*)"]}})
    )
    assert config.provider.claude_code.implementer_bash_rules == ["Bash(uv run pytest:*)"]


def test_non_mapping_is_rejected():
    with pytest.raises(ConfigError):
        parse_config(["not", "a", "mapping"])
