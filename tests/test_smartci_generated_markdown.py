import pytest

from textual.app import App, ComposeResult
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownTableContent


class TableApp(App[None]):
    def compose(self) -> ComposeResult:
        yield Markdown()


@pytest.mark.asyncio
async def test_append_completes_table_row_and_adds_another():
    app = TableApp()
    async with app.run_test():
        markdown = app.query_one(Markdown)
        await markdown.update('| Item | State |\n| --- | --- |\n| A | ready |\n| B | pen')
        table = markdown.query_one(MarkdownTableContent)
        last = table.last_row
        earlier = list(table.query_children(f'.cell.row{last - 1}'))
        assert len(earlier) == 2

        await markdown.append('ding |\n| C | done |')

        assert list(table.query_children(f'.cell.row{last - 1}')) == earlier
        assert table.last_row == last + 1
        for index, expected in [(last, ['B', 'pending']), (last + 1, ['C', 'done'])]:
            cells = table.query_children(f'.cell.row{index}')
            assert [cell.tooltip.plain for cell in cells] == expected
