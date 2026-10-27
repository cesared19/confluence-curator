import pytest

from curator import storage

BODY = (
    "<p>Intro</p>"
    "<h2>Encendido</h2><p>Botón POWER 3 s.</p>"
    '<h3 id="x">Detalle</h3><ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[a < b]]></ac:plain-text-body></ac:structured-macro>'
    "<h2>Alarmas</h2><p>8 bar&nbsp;máx.</p>"
)


def test_split_sections_by_heading():
    sections = storage.split_sections(BODY)
    assert [s.heading for s in sections] == ["(Introducción)", "Encendido", "Detalle", "Alarmas"]
    assert [s.level for s in sections] == [0, 2, 3, 2]
    assert "".join(s.storage for s in sections) == BODY


def test_replace_section_keeps_the_rest_byte_for_byte():
    sections = storage.split_sections(BODY)
    new = storage.replace_section(BODY, sections[3], "<h2>Alarmas</h2><p>7,5 bar</p>")
    assert new.startswith(BODY[: sections[3].start])
    assert new.endswith("<p>7,5 bar</p>")
    assert "<![CDATA[a < b]]>" in new


def test_layout_boundaries_are_not_crossed():
    body = (
        "<ac:layout><ac:layout-section ac:type=\"two_equal\">"
        "<ac:layout-cell><h2>Izquierda</h2><p>uno</p></ac:layout-cell>"
        "<ac:layout-cell><p>dos</p></ac:layout-cell>"
        "</ac:layout-section></ac:layout>"
    )
    sections = storage.split_sections(body)
    assert [s.storage for s in sections] == ["<h2>Izquierda</h2><p>uno</p>", "<p>dos</p>"]
    assert sections[1].heading == "Izquierda (cont.)"


def test_find_section_by_content_after_page_changed():
    sections = storage.split_sections(BODY)
    shifted = "<h2>Nueva</h2><p>x</p>" + BODY
    found = storage.find_section(shifted, sections[3].storage, 3)
    assert found is not None and found.heading == "Alarmas"
    assert storage.find_section(shifted, "<h2>No existe</h2>", 3) is None


def test_validate_fragment():
    storage.validate_fragment('<p>Hola&nbsp;<ac:image><ri:attachment ri:filename="a.png" /></ac:image></p>')
    with pytest.raises(ValueError):
        storage.validate_fragment("<p>sin cerrar")
    with pytest.raises(ValueError):
        storage.validate_fragment("<p>a & b</p>")


def test_placeholders():
    out = storage.replace_placeholders("<p>[[IMAGEN:2]]</p>", lambda n: storage.image_markup(f"f{n}.png"))
    assert 'ri:filename="f2.png"' in out
    storage.validate_fragment(out)
