from bibit_intent.core.command_contracts import side_effect_contract_allows
from bibit_intent.core.request_semantics import is_capability_question


def test_capability_questions_are_distinguished_from_file_queries():
    capability_inputs = (
        "hi,bibit，你有filesystem相关的mcp工具吗",
        "你有哪些 MCP 工具",
        "有没有文件搜索能力",
        "what tools do you have",
        "do you support filesystem",
    )
    for text in capability_inputs:
        assert is_capability_question(text), text

    file_queries = (
        "本地有没有周报",
        "电脑上找一下 filesystem 文档",
        "本地搜索 GTK 相关文件",
        "find local documents about filesystem",
    )
    for text in file_queries:
        assert not is_capability_question(text), text


def test_wifi_execution_requires_explicit_action_and_network_object():
    assert not side_effect_contract_allows("wifi_connect", "密码是bianbu")
    assert not side_effect_contract_allows("wifi_connect", "我的密码是12345678")
    assert side_effect_contract_allows("wifi_connect", "连接 JDSK 的 wifi，密码是12345678")
    assert side_effect_contract_allows("wifi_connect", "connect to Wi-Fi HomeNet")

