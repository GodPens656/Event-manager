import flet as ft

from Frontend.app import EventPlannerApp


def main(page: ft.Page) -> None:
    EventPlannerApp(page).build()


if __name__ == "__main__":
    ft.run(main)

