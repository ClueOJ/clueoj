from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TestCase, override_settings
from lxml import html

from judge.models import Problem, ProblemGroup
from . import fragment_tree_to_str, fragments_to_tree, get_cleaner, markdown

MATHML_N = """\
<math xmlns="http://www.w3.org/1998/Math/MathML">
<semantics>
<mi>N</mi>
<annotation encoding="application/x-tex">N</annotation>
</semantics>
</math>
"""

MATHML_CHUDNOVSKY = r"""
<math xmlns="http://www.w3.org/1998/Math/MathML"
      alttext="{\displaystyle {\frac {1}{\pi }}=12\sum _{k=0}^{\infty }{\frac {(-1)^{k}(6k)!(545140134k+13591409)}{(3k)!(k!)^{3}\left(640320\right)^{3k+3/2}}}}">
  <semantics>
    <mrow class="MJX-TeXAtom-ORD">
      <mstyle displaystyle="true" scriptlevel="0">
        <mrow class="MJX-TeXAtom-ORD">
          <mfrac>
            <mn>1</mn>
            <mi>π<!-- π --></mi>
          </mfrac>
        </mrow>
        <mo>=</mo>
        <mn>12</mn>
        <munderover>
          <mo>∑<!-- ∑ --></mo>
          <mrow class="MJX-TeXAtom-ORD">
            <mi>k</mi>
            <mo>=</mo>
            <mn>0</mn>
          </mrow>
          <mrow class="MJX-TeXAtom-ORD">
            <mi mathvariant="normal">∞<!-- ∞ --></mi>
          </mrow>
        </munderover>
        <mrow class="MJX-TeXAtom-ORD">
          <mfrac>
            <mrow>
              <mo stretchy="false">(</mo>
              <mo>−<!-- − --></mo>
              <mn>1</mn>
              <msup>
                <mo stretchy="false">)</mo>
                <mrow class="MJX-TeXAtom-ORD">
                  <mi>k</mi>
                </mrow>
              </msup>
              <mo stretchy="false">(</mo>
              <mn>6</mn>
              <mi>k</mi>
              <mo stretchy="false">)</mo>
              <mo>!</mo>
              <mo stretchy="false">(</mo>
              <mn>545140134</mn>
              <mi>k</mi>
              <mo>+</mo>
              <mn>13591409</mn>
              <mo stretchy="false">)</mo>
            </mrow>
            <mrow>
              <mo stretchy="false">(</mo>
              <mn>3</mn>
              <mi>k</mi>
              <mo stretchy="false">)</mo>
              <mo>!</mo>
              <mo stretchy="false">(</mo>
              <mi>k</mi>
              <mo>!</mo>
              <msup>
                <mo stretchy="false">)</mo>
                <mrow class="MJX-TeXAtom-ORD">
                  <mn>3</mn>
                </mrow>
              </msup>
              <msup>
                <mrow>
                  <mo>(</mo>
                  <mn>640320</mn>
                  <mo>)</mo>
                </mrow>
                <mrow class="MJX-TeXAtom-ORD">
                  <mn>3</mn>
                  <mi>k</mi>
                  <mo>+</mo>
                  <mn>3</mn>
                  <mrow class="MJX-TeXAtom-ORD">
                    <mo>/</mo>
                  </mrow>
                  <mn>2</mn>
                </mrow>
              </msup>
            </mrow>
          </mfrac>
        </mrow>
      </mstyle>
    </mrow>
    <annotation encoding="application/x-tex">{\displaystyle {\frac {1}{\pi }}=12\sum _{k=0}^{\infty }{\frac {(-1)^{k}(6k)!(545140134k+13591409)}{(3k)!(k!)^{3}\left(640320\right)^{3k+3/2}}}}</annotation>
  </semantics>
</math>
"""  # noqa: E501


class TestMarkdown(SimpleTestCase):
    BLEACHED_STYLE = 'problem'
    UNBLEACHED_STYLE = 'problem-full'

    def test_bleach(self):
        self.assertHTMLEqual(markdown('<script>void(0)</script>', self.BLEACHED_STYLE),
                             '&lt;script&gt;void(0)&lt;/script&gt;')
        self.assertHTMLEqual(markdown('<img style="display: block; margin: 0 auto">', self.BLEACHED_STYLE),
                             '<p><img style="display: block; margin: 0 auto;"></p>')

    def test_problem_style_elements_and_css_are_removed(self):
        source = ('<STYLE media="screen">#page-container { padding-top: 84px; }</STYLE>\n'
                  '<div class="banner">Notice</div>\n'
                  '<style>.banner { position: fixed; z-index: 450; }</style>\n'
                  '<p>Statement</p>')
        for style in (self.BLEACHED_STYLE, self.UNBLEACHED_STYLE):
            with self.subTest(style=style):
                result = markdown(source, style)
                tree = fragments_to_tree(result)
                self.assertFalse(tree.xpath('.//style'))
                self.assertNotIn('padding-top', result)
                self.assertNotIn('position: fixed', result)
                self.assertIn('Notice', tree.text_content())
                self.assertIn('Statement', tree.text_content())
                self.assertEqual(tree.xpath('.//div/@class'), ['banner'])

    def test_removing_problem_style_preserves_surrounding_text(self):
        for style in (self.BLEACHED_STYLE, self.UNBLEACHED_STYLE):
            with self.subTest(style=style):
                self.assertHTMLEqual(markdown('<p>before<style>p { color: red; }</style>after</p>', style),
                                     '<p>beforeafter</p>')

    def test_unclosed_problem_style_is_removed(self):
        for style in (self.BLEACHED_STYLE, self.UNBLEACHED_STYLE):
            with self.subTest(style=style):
                tree = fragments_to_tree(markdown('<style>body { display: none; }', style))
                self.assertFalse(tree.xpath('.//style'))
                self.assertEqual(tree.text_content().strip(), '')

    def test_problem_inline_styles_are_preserved(self):
        source = '<p style="text-align: center">Statement</p>'
        for style in (self.BLEACHED_STYLE, self.UNBLEACHED_STYLE):
            with self.subTest(style=style):
                tree = fragments_to_tree(markdown(source, style))
                self.assertIn('text-align: center', tree.xpath('.//p/@style')[0])

    def test_literal_style_elements_in_code_are_preserved(self):
        for source in ('`<style>p { color: red; }</style>`',
                       '```html\n<style>p { color: red; }</style>\n```'):
            for style in (self.BLEACHED_STYLE, self.UNBLEACHED_STYLE):
                with self.subTest(style=style, source=source):
                    tree = fragments_to_tree(markdown(source, style))
                    self.assertFalse(tree.xpath('.//style'))
                    self.assertIn('<style>p { color: red; }</style>', tree.text_content())

    def test_non_problem_stylesheets_are_unchanged(self):
        for style in ('contest', 'blog', 'solution', 'flatpage'):
            with self.subTest(style=style):
                tree = fragments_to_tree(markdown('<style>p { color: red; }</style>', style))
                self.assertEqual(tree.xpath('.//style/text()'), ['p { color: red; }'])

    def test_bleach_mathml(self):
        self.assertHTMLEqual(markdown(MATHML_N, self.BLEACHED_STYLE), MATHML_N)
        cleaner = get_cleaner(self.BLEACHED_STYLE, None)
        self.assertHTMLEqual(cleaner.clean(MATHML_CHUDNOVSKY), MATHML_CHUDNOVSKY)

    def test_no_bleach(self):
        self.assertHTMLEqual(markdown('<script>void(0)</script>', self.UNBLEACHED_STYLE),
                             '<script>void(0)</script>')

    def test_post_process(self):
        self.assertHTMLEqual(markdown('<img src="test.png">', self.UNBLEACHED_STYLE, lazy_load=True),
                             '<p><noscript><img src="test.png"></noscript>'
                             '<img src="/static/blank.gif" data-src="test.png" class="unveil"></p>')


class TestFragmentUtils(SimpleTestCase):
    def test_simple(self):
        tree = fragments_to_tree('<p>a</p><p>b</p>')
        self.assertIsInstance(tree, html.HtmlElement)
        self.assertEqual(len(tree.getchildren()), 2)

        self.assertIsInstance(tree[0], html.HtmlElement)
        self.assertEqual(tree[0].tag, 'p')
        self.assertEqual(tree[0].text, 'a')

        self.assertIsInstance(tree[1], html.HtmlElement)
        self.assertEqual(tree[1].tag, 'p')
        self.assertEqual(tree[1].text, 'b')

        self.assertHTMLEqual(fragment_tree_to_str(tree), '<p>a</p><p>b</p>')

    def test_text_prefix(self):
        tree = fragments_to_tree('z<p>a</p><p>b</p>')
        self.assertIsInstance(tree, html.HtmlElement)
        self.assertEqual(len(tree.getchildren()), 2)
        self.assertEqual(tree.text, 'z')

        self.assertHTMLEqual(fragment_tree_to_str(tree), 'z<p>a</p><p>b</p>')


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class TestProblemStatementCache(TestCase):
    def test_statement_cache_ignores_old_stylesheets_and_invalidates_on_edit(self):
        source = '<style>body { display: none; }</style>\n\nOriginal statement'
        problem = Problem.objects.create(
            code='statement_style_cache', name='Statement cache', description=source,
            group=ProblemGroup.objects.create(name='statement', full_name='Statements'),
            time_limit=1, memory_limit=65536, points=100,
        )
        cache.set(make_template_fragment_key('problem_html', (problem.pk, 'tex', 'en')),
                  '<style>body { display: none; }</style>Stale statement', 86400)

        def render_statement():
            return render_to_string('problem/problem-detail.html', {
                'problem': problem,
                'description': problem.description,
                'MATH_ENGINE': 'tex',
                'LANGUAGE_CODE': 'en',
            })

        rendered = render_statement()
        self.assertIn('Original statement', rendered)
        self.assertNotIn('Stale statement', rendered)
        self.assertNotIn('<style', rendered)
        self.assertNotIn('display: none', rendered)
        problem.refresh_from_db()
        self.assertEqual(problem.description, source)

        problem.description = 'Updated statement'
        problem.save(update_fields=['description'])
        rendered = render_statement()
        self.assertIn('Updated statement', rendered)
        self.assertNotIn('Original statement', rendered)
