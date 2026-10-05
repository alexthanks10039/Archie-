from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from backup import (
    chapter_numbers_from_anchor,
    group_chapter_links,
    split_merged_chapters,
    save_merged_chapters,
)


class TestMergedChapterParsing(unittest.TestCase):
    def test_plain_chapter_anchor(self) -> None:
        soup = BeautifulSoup(
            '<a href="/glava-1860-test/">Глава 1860</a>',
            "lxml",
        )
        self.assertEqual(chapter_numbers_from_anchor(soup.a), [1860])

    def test_merged_chapter_anchor(self) -> None:
        soup = BeautifulSoup(
            '<a href="/glava-1855-test/">Глава 1854-1855</a>',
            "lxml",
        )
        self.assertEqual(chapter_numbers_from_anchor(soup.a), [1854, 1855])

    def test_merged_anchor_does_not_trust_url_number(self) -> None:
        soup = BeautifulSoup(
            '<a href="/glava-1855-test/">Глава 1854-1855</a>',
            "lxml",
        )
        numbers = chapter_numbers_from_anchor(soup.a)
        self.assertNotEqual(numbers, [1855])
        self.assertEqual(numbers, [1854, 1855])

    def test_real_range_numbers_1849_to_1856(self) -> None:
        soup = BeautifulSoup(
            """
            <div>
              <a href="/x/glava-1850/">Глава 1849-1850</a>
              <a href="/x/glava-1851/">Глава 1851</a>
              <a href="/x/glava-1852/">Глава 1852-1853</a>
              <a href="/x/glava-1855/">Глава 1854-1855</a>
              <a href="/x/glava-1856/">Глава 1856</a>
            </div>
            """,
            "lxml",
        )
        discovered = []
        for anchor in soup.find_all("a"):
            discovered.extend(chapter_numbers_from_anchor(anchor))
        self.assertEqual(sorted(discovered), list(range(1849, 1857)))

    def test_grouping_fetches_merged_source_once(self) -> None:
        links = {
            1854: "https://ifreedom.su/x/glava-1855/",
            1855: "https://ifreedom.su/x/glava-1855/",
            1856: "https://ifreedom.su/x/glava-1856/",
        }
        self.assertEqual(
            group_chapter_links(links),
            {
                "https://ifreedom.su/x/glava-1855/": [1854, 1855],
                "https://ifreedom.su/x/glava-1856/": [1856],
            },
        )

    def test_heading_with_title_suffix_is_detected(self) -> None:
        html = """
        <article>
          <h2>Глава 1854-Мастер города Чистилища 5</h2>
          <p>First chapter body with enough text to pass the parser validation and demonstrate the first independent chapter segment correctly.</p>
          <p>More first chapter material makes the separation test realistic enough for the minimum length guard.</p>
          <h2>Глава 1855-Мастер города Чистилища 6</h2>
          <p>Second chapter body with enough text to pass the parser validation and demonstrate the second independent chapter segment correctly.</p>
          <p>More second chapter material makes the separation test realistic enough for the minimum length guard.</p>
        </article>
        """
        result = split_merged_chapters(html, [1854, 1855])
        self.assertEqual(set(result), {1854, 1855})

    def test_split_merged_html(self) -> None:
        html = """
        <html><body>
        <article class="entry-content">
          <h1>Глава 1854-1855: Мастер города Чистилища</h1>
          <h2>Глава 1854-Мастер города Чистилища 5</h2>
          <p>Текст первой главы. Разные события происходят здесь.</p>
          <p>Ещё один абзац первой главы.</p>
          <h2>Глава 1855-Мастер города Чистилища 6</h2>
          <p>Текст второй главы. Это уже совершенно другой фрагмент.</p>
          <p>Ещё один абзац второй главы.</p>
        </article>
        </body></html>
        """
        result = split_merged_chapters(html, [1854, 1855])

        self.assertEqual(set(result), {1854, 1855})
        self.assertIn("первой главы", result[1854]["text"])
        self.assertNotIn("второй главы", result[1854]["text"])
        self.assertIn("второй главы", result[1855]["text"])
        self.assertNotIn("первой главы", result[1855]["text"])
        self.assertNotEqual(result[1854]["text"], result[1855]["text"])

    def test_range_heading_alone_is_not_enough(self) -> None:
        html = """
        <article class="entry-content">
          <h1>Глава 1854-1855</h1>
          <p>Объединённый текст без внутренних заголовков.</p>
        </article>
        """
        with self.assertRaises(ValueError):
            split_merged_chapters(html, [1854, 1855])

    def test_save_merged_creates_separate_files(self) -> None:
        html = """
        <article>
          <h1>Глава 1854-1855</h1>
          <h2>Глава 1854</h2>
          <p>Alpha text that is long enough for the chapter validation.</p>
          <p>More alpha content here.</p>
          <h2>Глава 1855</h2>
          <p>Beta text that is long enough for the chapter validation.</p>
          <p>More beta content here.</p>
        </article>
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            records = save_merged_chapters(
                html,
                "https://ifreedom.su/glava-1855-test/",
                [1854, 1855],
                directory,
            )
            self.assertEqual(set(records), {1854, 1855})
            first = (directory / "1854.txt").read_text(encoding="utf-8")
            second = (directory / "1855.txt").read_text(encoding="utf-8")
            self.assertNotEqual(first, second)
            self.assertIn("Alpha", first)
            self.assertIn("Beta", second)
            self.assertNotIn("Beta", first)
            self.assertNotIn("Alpha", second)


if __name__ == "__main__":
    unittest.main()
