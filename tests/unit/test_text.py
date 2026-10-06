import pytest

from jobradar.text import html_to_text


def test_escaped_greenhouse_content():
    text = html_to_text("&lt;h2&gt;Role&lt;/h2&gt;&lt;p&gt;Use &amp;amp; love &lt;b&gt;Java&lt;/b&gt;&lt;/p&gt;"
                        "&lt;ul&gt;&lt;li&gt;one&lt;/li&gt;&lt;li&gt;two&lt;/li&gt;&lt;/ul&gt;")
    assert text == "Role\n\nUse & love Java\n\n- one\n- two"


def test_unescaped_html_gives_same_result_as_escaped():
    assert html_to_text("<p>a &amp; b</p>") == html_to_text("&lt;p&gt;a &amp;amp; b&lt;/p&gt;") == "a & b"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_input(value):
    assert html_to_text(value) == ""


def test_plain_text_passes_through():
    assert html_to_text("Just text, no tags.") == "Just text, no tags."


def test_script_and_style_content_is_dropped():
    assert html_to_text("<p>a</p><script>alert(1)</script><style>p{color:red}</style><p>b</p>") == "a\n\nb"


def test_br_and_nbsp_and_whitespace_are_normalized():
    assert html_to_text("<p>line&nbsp;one<br>line   two</p>") == "line one\nline two"


def test_blank_lines_are_collapsed():
    assert "\n\n\n" not in html_to_text("<div><div><p>a</p></div></div><div><p>b</p></div>")


def test_nested_list_items_each_on_own_line():
    text = html_to_text("<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>")
    assert [l for l in text.splitlines() if l] == ["- a", "- b", "- c"]


def test_paragraphs_inside_list_items_stay_on_the_bullet_line():
    html = "<h2>Requirements</h2><ul><li><p>BS in CS</p></li><li><p>0-2 years</p></li></ul><p>Next</p>"
    assert html_to_text(html) == "Requirements\n\n- BS in CS\n- 0-2 years\n\nNext"
