import flet as ft

from Frontend.app import EventPlannerApp


async def main(page: ft.Page) -> None:
    await EventPlannerApp(page).build()


if __name__ == "__main__":
    ft.run(
        main,
        view=ft.AppView.WEB_BROWSER,
        host="127.0.0.1",
        port=8000,
        assets_dir=None,
        no_cdn=True,
    )
