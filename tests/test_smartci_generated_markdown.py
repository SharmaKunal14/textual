import pytest

from textual.app import App, ComposeResult
from textual.content import Content
from textual.widgets import Markdown
from textual.widgets._markdown import MarkdownTableContent


class TableApp(App[None]):
    def compose(self) -> ComposeResult:
        yield Markdown()


@pytest.mark.asyncio
async def test_update_rows_replaces_only_the_last_row_and_extends_table():
    app = TableApp()
    async with app.run_test():
        markdown = app.query_one(Markdown)
        await markdown.update('| Item | State |\n| --- | --- |\n| A | ready |\n| B | pen |')
        table = markdown.query_one(MarkdownTableContent)
        last = table.last_row

        def row_values(index: int) -> list[str]:
            return [cell.tooltip.plain for cell in table.query_children(f'.cell.row{index}')]

        assert row_values(last) == ['B', 'pen']
        await table._update_rows([
            [Content('B'), Content('pending')],
            [Content('C'), Content('done')],
        ])

        assert table.styles.grid_size_columns == 2
        assert table.last_row == last + 1
        assert row_values(last - 1) == ['A', 'ready']
        assert row_values(last) == ['B', 'pending']
        assert row_values(last + 1) == ['C', 'done']
