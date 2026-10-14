from pathlib import Path


POSTINST = Path(__file__).resolve().parents[1] / "debian/postinst"


def test_bibit_postinst_restarts_active_runtime_after_mcp_update():
    text = POSTINST.read_text(encoding="utf-8")
    assert "bianbu-agent-runtime.service" in text
    assert "systemctl is-active --quiet bianbu-agent-runtime.service" in text
    assert "systemctl restart bianbu-agent-runtime.service" in text
    assert "systemctl daemon-reload" in text


def test_bibit_postinst_does_not_start_stopped_runtime():
    text = POSTINST.read_text(encoding="utf-8")
    guard = "if [ -d /run/systemd/system ] && systemctl is-active --quiet bianbu-agent-runtime.service; then"
    start = text.index(guard)
    end = text.index("fi", start)
    block = text[start:end]
    assert "systemctl start bianbu-agent-runtime.service" not in block
