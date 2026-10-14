from bibit_intent.core.package_target import split_exact_install_specs, split_exact_packages


def test_split_exact_packages_accepts_common_separators_and_deduplicates():
    assert split_exact_packages("stress-ng iperf3") == ["stress-ng", "iperf3"]
    assert split_exact_packages("stress-ng,iperf3") == ["stress-ng", "iperf3"]
    assert split_exact_packages("stress-ng，iperf3") == ["stress-ng", "iperf3"]
    assert split_exact_packages("stress-ng and iperf3") == ["stress-ng", "iperf3"]
    assert split_exact_packages("iperf3 stress-ng iperf3") == ["iperf3", "stress-ng"]


def test_split_exact_packages_rejects_non_exact_parts():
    assert split_exact_packages("stress-ng vim*") is None
    assert split_exact_packages("stress-ng --purge") is None
    assert split_exact_packages("") is None
    assert split_exact_packages(None) is None


def test_split_preserves_suffix_names_for_exact_version_validation():
    assert split_exact_packages("gcc g++ g++:riscv64 git-") == [
        "gcc", "g++", "g++:riscv64", "git-",
    ]


def test_split_exact_install_specs_accepts_explicit_versions_and_deduplicates():
    assert split_exact_install_specs("vim git=1:2.34.1-1") == [
        ("vim", None), ("git", "1:2.34.1-1"),
    ]
    assert split_exact_install_specs("git=1:2.34.1-1 git=1:2.30.0-1") == [("git", "1:2.34.1-1")]


def test_split_exact_install_specs_rejects_invalid_specs():
    assert split_exact_install_specs("git=1.*") is None
    assert split_exact_install_specs("git=") is None
    assert split_exact_install_specs("") is None
