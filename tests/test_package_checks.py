from types import SimpleNamespace

import pytest

from bibit_intent.core import package_checks
from bibit_intent.core.package_state import PackageState


@pytest.fixture
def queries(monkeypatch):
    calls = []
    def query(args):
        calls.append(args)
        outputs = {
            "policy": "libfoo:\n  Installed: (none)\n  Candidate: 1.2-1\n  Version table:\n     1.2-1 500\n        500 http://example.invalid stable/main riscv64 Packages\n",
            "show": "Package: libfoo\nVersion: 1.2-1\nArchitecture: riscv64\nEssential: no\n\n",
            "--print-architecture": "riscv64\n",
            "--print-foreign-architectures": "arm64\n",
            "-s": "Inst libfoo [1.0] (1.2-1 Repo [riscv64])\nInst libfoo-dep (2.0 Repo [riscv64])\nRemv obsolete [0.1]\nConf libfoo (1.2-1 Repo [riscv64])\n",
        }
        return SimpleNamespace(returncode=0, stdout=outputs.get(args[1], ""), stderr="")
    monkeypatch.setattr(package_checks, "_query", query)
    monkeypatch.setattr(package_checks, "query_package_state", lambda pkg: PackageState("installed", "1.2-1", True))
    return calls


def test_candidate_has_facts_not_installability_claim(queries):
    result = package_checks.package_candidate("libfoo")
    assert result["ok"] and result["candidate"] == "1.2-1"
    assert result["items"][0]["architecture_compatible"]
    assert result["installability"] == "not_verified"
    assert "example.invalid" in result["policy"]
    assert result["local_state"]["held"]
    assert all(args[0] in {"apt-cache", "dpkg"} for args in queries)


@pytest.mark.parametrize("policy", ["", "  Candidate: (none)\n", "  Candidate: 1\n  Candidate: 2\n"])
def test_candidate_missing_or_ambiguous_is_unknown(monkeypatch, policy):
    monkeypatch.setattr(package_checks, "_query", lambda args: SimpleNamespace(returncode=0, stdout=policy, stderr=""))
    result = package_checks.package_candidate("libfoo")
    assert result["candidate"] is None and result["installability"] == "unknown"


def test_simulation_preserves_changes_and_boundaries(queries):
    result = package_checks.package_operation_preview("libfoo", "install")
    assert result["ok"] and result["read_only"] and result["parse_complete"]
    assert result["changes"][0]["old_version"] == "1.0"
    assert result["changes"][1]["new_version"] == "2.0"
    assert result["changes"][2]["action"] == "remove"
    assert result["protected_policy"] == "not_checked" and result["disk_delta_bytes"] is None
    assert queries == [["apt-get", "-s", "-o", "Debug::NoLocking=1", "--no-remove", "install", "--", "libfoo"]]


def test_action_suffix_policy_must_resolve_the_requested_package(monkeypatch):
    calls = []

    def query(args):
        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout="swell-foop:\n  Installed: (none)\n  Candidate: 1:48.0-1\n",
            stderr="",
        )

    monkeypatch.setattr(package_checks, "_query", query)
    result = package_checks.package_candidate("git-")
    assert not result["ok"]
    assert "完全一致" in result["message"]
    assert calls == [["apt-cache", "policy", "git-"]]


def test_valid_plus_package_is_pinned_before_simulation(monkeypatch):
    calls = []

    def query(args):
        calls.append(args)
        output = {
            ("apt-cache", "policy", "g++"): "g++:\n  Installed: (none)\n  Candidate: 4:14.2.0-1\n",
            ("apt-cache", "show", "--no-all-versions", "g++=4:14.2.0-1"): "Package: g++\nVersion: 4:14.2.0-1\nArchitecture: amd64\n\n",
            ("dpkg", "--print-architecture"): "amd64\n",
            ("dpkg", "--print-foreign-architectures"): "",
        }.get(tuple(args), "")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(package_checks, "_query", query)
    monkeypatch.setattr(package_checks, "query_package_state", lambda pkg: PackageState("absent"))
    assert package_checks.apt_install_target("g++") == "g++=4:14.2.0-1"
    assert all(args[0] != "apt-get" for args in calls)


def test_simulation_does_not_require_cached_archives(queries, monkeypatch):
    calls = []
    def query(args):
        calls.append(args)
        assert args[:2] == ["apt-get", "-s"]
        if "--no-download" in args:
            return SimpleNamespace(returncode=100, stdout="", stderr="Unable to fetch some archives")
        return SimpleNamespace(returncode=0, stdout="Inst libfoo [1.0] (1.2-1 . resolute-porting:resolute-porting [riscv64])\nConf libfoo (1.2-1 . resolute-porting:resolute-porting [riscv64])\n", stderr="")
    monkeypatch.setattr(package_checks, "_query", query)
    result = package_checks.package_operation_preview("libfoo", "install")
    assert result["ok"] and result["parse_complete"]
    assert result["changes"][0]["new_version"] == "1.2-1"
    assert len(calls) == 1 and "--no-download" not in calls[0]


def test_unparsed_simulation_not_complete(queries, monkeypatch):
    monkeypatch.setattr(package_checks, "_query", lambda args: SimpleNamespace(returncode=0, stdout="Inst bad format ??\n", stderr=""))
    result = package_checks.package_operation_preview("libfoo", "remove")
    assert not result["ok"] and not result["parse_complete"]


@pytest.mark.parametrize("pkg", ["bash*", "--purge", "vim bash", "^bash.*$"])
def test_no_queries_for_nonexact_targets(monkeypatch, pkg):
    def forbidden(*args):
        pytest.fail("invalid target must not run a command")
    monkeypatch.setattr(package_checks, "_query", forbidden)
    monkeypatch.setattr(package_checks, "query_package_state", forbidden)
    assert not package_checks.package_candidate(pkg)["ok"]
    assert not package_checks.package_operation_preview(pkg, "install")["ok"]
    assert not package_checks.verify_package(pkg)["ok"]


@pytest.mark.parametrize("status", ["unknown", "incomplete", "config_only", "absent"])
def test_verification_never_calls_uninstalled_success(monkeypatch, status):
    monkeypatch.setattr(package_checks, "query_package_state", lambda pkg: PackageState(status))
    result = package_checks.verify_package("libfoo", "1.0")
    assert not result["ok"] and result["version_matches"] is None


@pytest.mark.parametrize("code,matched,ok", [(0, True, True), (1, False, False)])
def test_expected_version_verification(queries, monkeypatch, code, matched, ok):
    calls = []
    def compare(args):
        calls.append(args)
        return SimpleNamespace(returncode=code, stdout="", stderr="")
    monkeypatch.setattr(package_checks, "_query", compare)
    result = package_checks.verify_package("libfoo", "1:1.0~rc1-2")
    assert result["ok"] is ok and result["version_matches"] is matched
    assert result["usability"] == "not_checked"
    assert calls == [["dpkg", "--compare-versions", "1.2-1", "eq", "1:1.0~rc1-2"]]


def test_version_unspecified_not_claimed_verified(queries):
    result = package_checks.verify_package("libfoo")
    assert result["ok"] and result["version_matches"] is None and not queries


def test_backend_failure_is_not_absence(queries, monkeypatch):
    monkeypatch.setattr(package_checks, "_query", lambda args: SimpleNamespace(returncode=100, stdout="", stderr="backend failed"))
    assert not package_checks.package_candidate("libfoo")["ok"]
    result = package_checks.package_operation_preview("libfoo", "install")
    assert not result["ok"] and "backend failed" in result["diagnostic"]


def _assessment_query(*, architecture="riscv64", policy_candidate="1.2-1", package_arch="riscv64", release="Bianbu 3.0", apt_rc=0):
    policy = f"libfoo:\n  Installed: (none)\n  Candidate: {policy_candidate}\n" if policy_candidate else "libfoo:\n  Installed: (none)\n  Candidate: (none)\n"
    def query(args):
        key = tuple(args)
        if key == ("dpkg", "--print-architecture"):
            return SimpleNamespace(returncode=0 if architecture else 1, stdout=f"{architecture}\n", stderr="arch failed" if not architecture else "")
        if key == ("lsb_release", "-ds"):
            return SimpleNamespace(returncode=0, stdout=f"{release}\n", stderr="")
        if key == ("apt-cache", "policy", "libfoo"):
            return SimpleNamespace(returncode=apt_rc, stdout=policy, stderr="backend failed" if apt_rc else "")
        if key == ("apt-cache", "show", "--no-all-versions", "libfoo=1.2-1"):
            return SimpleNamespace(returncode=0, stdout=f"Package: libfoo\nVersion: 1.2-1\nArchitecture: {package_arch}\n\n", stderr="")
        if key == ("dpkg", "--print-foreign-architectures"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if key == ("uname", "-m"):
            return SimpleNamespace(returncode=0, stdout="riscv64\n", stderr="")
        if key == ("getconf", "GNU_LIBC_VERSION"):
            return SimpleNamespace(returncode=0, stdout="glibc 2.39\n", stderr="")
        if key == ("getconf", "LONG_BIT"):
            return SimpleNamespace(returncode=0, stdout="64\n", stderr="")
        raise AssertionError(f"unexpected query: {args}")
    return query


def test_delivery_assessment_reports_riscv_candidate(monkeypatch):
    monkeypatch.setattr(package_checks, "_query", _assessment_query())
    monkeypatch.setattr(package_checks, "query_package_state", lambda pkg: PackageState("absent"))
    result = package_checks.software_delivery_assess("libfoo", scope="system")
    assert result["ok"] and result["read_only"]
    assert result["diagnosis"] == "repo_candidate_found"
    assert result["architecture"] == "riscv64"
    assert result["machine"] == "riscv64"
    assert result["machine_query"] == "ok"
    assert result["abi"] == {"libc": "glibc", "libc_version": "2.39", "word_size": "64", "status": "complete"}
    assert result["foreign_architectures"] == []
    assert result["foreign_architectures_query"] == "ok"
    assert result["distribution"] == "Bianbu 3.0"
    assert result["installable"] == "not_verified"
    assert result["third_party_candidates"] == []
    assert result["source_build"] == "not_assessed"
    assert result["network_accessed"] is False and result["write_performed"] is False


def test_delivery_assessment_distinguishes_missing_candidate(monkeypatch):
    monkeypatch.setattr(package_checks, "_query", _assessment_query(policy_candidate=None))
    result = package_checks.software_delivery_assess("libfoo")
    assert result["ok"] and result["diagnosis"] == "candidate_not_found"
    assert result["installable"] == "unknown"


def test_delivery_assessment_distinguishes_architecture_mismatch(monkeypatch):
    monkeypatch.setattr(package_checks, "_query", _assessment_query(package_arch="amd64"))
    monkeypatch.setattr(package_checks, "query_package_state", lambda pkg: PackageState("absent"))
    result = package_checks.software_delivery_assess("libfoo")
    assert result["ok"] and result["diagnosis"] == "repo_package_unavailable_for_arch"
    assert result["installable"] == "unknown"


def test_delivery_assessment_does_not_turn_apt_failure_into_absence(monkeypatch):
    monkeypatch.setattr(package_checks, "_query", _assessment_query(apt_rc=100))
    result = package_checks.software_delivery_assess("libfoo")
    assert not result["ok"] and result["diagnosis"] == "repository_configuration_unknown"
    assert "backend failed" in result["diagnostic"]


def test_delivery_assessment_handles_architecture_failure(monkeypatch):
    monkeypatch.setattr(package_checks, "_query", _assessment_query(architecture=""))
    result = package_checks.software_delivery_assess("libfoo")
    assert not result["ok"] and result["diagnosis"] == "architecture_unknown"


@pytest.mark.parametrize("target", ["bash*", "^bash.*$", "vim bash", "https://example.invalid/foo"])
def test_delivery_assessment_rejects_nonexact_target_without_queries(monkeypatch, target):
    monkeypatch.setattr(package_checks, "_query", lambda args: pytest.fail("invalid target must not run a command"))
    result = package_checks.software_delivery_assess(target)
    assert not result["ok"] and result["diagnosis"] == "invalid_target"


def test_delivery_assessment_validates_version_and_scope(monkeypatch):
    monkeypatch.setattr(package_checks, "_query", lambda args: pytest.fail("invalid input must not run a command"))
    assert package_checks.software_delivery_assess("libfoo", version="https://bad") ["diagnosis"] == "invalid_version"
    assert package_checks.software_delivery_assess("libfoo", scope="arbitrary") ["diagnosis"] == "invalid_scope"


@pytest.mark.parametrize("owner,ok", [("libfoo: /usr/bin/foo", True), ("other: /usr/bin/foo", False)])
def test_optional_executable_check_does_not_launch(queries, monkeypatch, owner, ok):
    monkeypatch.setattr(package_checks.shutil, "which", lambda command: "/usr/bin/foo")
    calls = []
    def query(args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=owner, stderr="")
    monkeypatch.setattr(package_checks, "_query", query)
    result = package_checks.verify_package("libfoo", executable="foo")
    assert result["ok"] is ok
    assert calls == [["dpkg-query", "-S", "/usr/bin/foo"]]
    assert result["usability"] == "not_checked"


def test_optional_executable_missing(queries, monkeypatch):
    monkeypatch.setattr(package_checks.shutil, "which", lambda command: None)
    result = package_checks.verify_package("libfoo", executable="foo")
    assert not result["ok"] and result["command_check"]["status"] == "not_found"
    assert not queries


def test_dependency_conflict_is_visible_without_repair(queries, monkeypatch):
    calls = []
    def query(args):
        calls.append(args)
        return SimpleNamespace(returncode=100, stdout="bianbu-agent-runtime : Depends: bibit-intent (>= 0.1.7) but 0.1.1 is to be installed\n", stderr="E: Unmet dependencies")
    monkeypatch.setattr(package_checks, "_query", query)
    result = package_checks.package_operation_preview("bash", "install")
    assert not result["ok"] and "0.1.7" in result["message"] and "0.1.1" in result["message"]
    assert len(calls) == 1 and "-s" in calls[0]
