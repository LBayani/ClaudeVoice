from voice_server import brain
from voice_server.speakable import split_sentences, to_speakable


def test_speakable_strips_markdown_and_links():
    t = to_speakable("**Готово**: см. [доку](https://example.com) и `код`")
    assert "*" not in t and "https" not in t and "доку" in t and "код" in t


def test_speakable_numbers_and_units():
    t = to_speakable("Свободно 15 ГБ, загрузка 3%")
    assert "пятнадцать" in t and "гигабайт" in t and "процентов" in t


def test_speakable_code_block():
    assert "Код показан на экране" in to_speakable("Вот:\n```py\nprint(1)\n```")


def test_split_sentences_long_and_latin_only():
    parts = split_sentences("Первое. Второе! " + "очень длинное, " * 30 + "конец. OK.")
    assert parts[0] == "Первое." and parts[1] == "Второе!"
    assert all(len(p) <= 220 for p in parts)
    assert "OK." not in parts          # nothing speakable in Russian


def test_sentence_end_streaming_regex():
    ends = [m.end() for m in brain._SENTENCE_END.finditer("Цена 3.5 рубля. Дальше")]
    assert ends == [len("Цена 3.5 рубля.")]       # "3.5" does not cut the stream, ". " does
