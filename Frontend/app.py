import asyncio
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

import flet as ft

from Backend.attachments import MAX_SCRIPT_BYTES, ScriptAttachment
from Backend.services import MailService, budget_totals, normalize_email, parse_amount
from Backend.storage import (
    DEFAULT_DATA, DEFAULT_EVENT, MAX_DATA_BYTES, BrowserStorage, StorageConflict,
    decode_data, encode_data,
)
from Backend.mailing import DEFAULT_TEMPLATES, MAIL_PROVIDERS, mail_settings, render_mail
from Frontend.mail_help import MAIL_HELP


class EventPlannerApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.preferences = ft.SharedPreferences()
        self.page.services.append(self.preferences)
        self.storage = BrowserStorage(self.preferences)
        self.store = deepcopy(DEFAULT_DATA)
        self._action_lock = asyncio.Lock()
        self.backup_picker = None
        self.data = None
        self.content = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO)
        self.password = ""
        self.script_attachment = None
        self.mail_picker = None
        self.sending = False
        self.send_buttons = []
        self.sidebar = None
        self.workspace_body = None
        self.mobile_header = None

    def clear_sensitive(self, _=None) -> None:
        self.password = ""
        self.script_attachment = None

    def action(self, handler):
        async def handle(event):
            async with self._action_lock:
                await handler(event)
        return handle

    async def save_store(self) -> bool:
        try:
            await self.storage.save(self.store)
            return True
        except StorageConflict as error:
            self.page.pop_dialog()
            await self.route_change(None)
            self.notice(str(error), True)
        except Exception as error:
            # A failed browser write must not leave unsaved rows in session state.
            event_id = self.data["id"] if self.data else None
            self.store = self.storage.last_loaded()
            self.data = next((item for item in self.store["events"] if item["id"] == event_id), None)
            self.notice(f"Не удалось сохранить данные в браузере: {error}", True)
        return False

    async def build(self) -> None:
        self.page.title = "Организатор мероприятий"
        # Flet follows system changes without rebuilding forms or losing input.
        self.page.theme_mode = ft.ThemeMode.SYSTEM
        self.page.theme = ft.Theme(
            color_scheme_seed=ft.Colors.INDIGO,
            color_scheme=ft.ColorScheme(
                tertiary="#256C2C",
                on_tertiary="#FFFFFF",
                tertiary_container="#B0F2AD",
                on_tertiary_container="#002204",
            ),
        )
        self.page.dark_theme = ft.Theme(
            color_scheme_seed=ft.Colors.INDIGO,
            color_scheme=ft.ColorScheme(
                tertiary="#95D693",
                on_tertiary="#00390A",
                tertiary_container="#075319",
                on_tertiary_container="#B0F2AD",
            ),
        )
        self.page.bgcolor = ft.Colors.SURFACE
        self.page.padding = 0
        self.page.on_route_change = self.action(self.route_change)
        self.page.on_resize = self.resize_workspace
        self.page.on_close = self.clear_sensitive
        self.page.add(ft.Text("Загрузка данных из браузера…"), ft.ProgressRing())
        await self.route_change(None)

    async def route_change(self, _) -> None:
        try:
            self.store = await self.storage.load()
        except Exception as error:
            self.clear_workspace()
            self.data = None
            self.page.clean()
            self.page.add(ft.Column([
                ft.Text("Не удалось загрузить данные из браузера", size=24),
                ft.Text(str(error)),
                ft.Text("Разрешите хранение данных сайта и повторите попытку. Существующие данные не будут заменены."),
                ft.Button("Повторить", on_click=self.action(self.route_change)),
            ]))
            return
        route = (self.page.route or "/").rstrip("/") or "/"
        if route == "/events/add":
            self.show_add_event_page()
            return
        if route.startswith("/event/"):
            parts = route.strip("/").split("/")
            if len(parts) < 2:
                self.page.go("/")
                return
            event_id = parts[1]
            event_data = next(
                (
                    item
                    for item in self.store["events"]
                    if item["id"] == event_id
                ),
                None,
            )
            if event_data is None:
                self.page.go("/")
                return
            if self.data is None or self.data["id"] != event_id:
                self.clear_sensitive()
            self.data = event_data
            page_name = parts[2] if len(parts) >= 3 else "event"
            sections = {
                "event": 0,
                "guests": 1,
                "team": 2,
                "budget": 3,
                "mail": 4,
            }
            section = sections.get(page_name)
            if section is None or len(parts) > 4:
                self.page.go(f"/event/{event_id}")
                return
            if len(parts) == 4:
                if parts[3] != "add" or page_name not in {
                    "guests",
                    "team",
                    "budget",
                }:
                    self.page.go(f"/event/{event_id}")
                    return
                add_views = {
                    "guests": lambda: self.person_add_view("guests", False),
                    "team": lambda: self.person_add_view("participants", True),
                    "budget": self.budget_add_view,
                }
                self.show_workspace(section, add_views[page_name]())
                return
            self.show_workspace(section)
            return
        if route != "/":
            self.page.go("/")
            return
        self.show_event_selector()

    def resize_workspace(self, _=None) -> None:
        if self.workspace_body is None:
            return
        compact = (self.page.width or 1024) < 760
        self.sidebar.visible = not compact
        self.mobile_header.visible = compact
        self.page.navigation_bar.visible = compact
        self.workspace_body.padding = 16 if compact else 28
        self.page.update()

    def clear_workspace(self) -> None:
        self.sidebar = None
        self.workspace_body = None
        self.mobile_header = None
        self.page.navigation_bar = None
        self.clear_sensitive()

    def event_route(self, section: str = "") -> str:
        base = f"/event/{self.data['id']}"
        return f"{base}/{section}" if section else base

    def go_to_section(self, index: int) -> None:
        sections = ("", "guests", "team", "budget", "mail")
        self.page.go(self.event_route(sections[index]))

    def show_workspace(
        self, selected_index: int = 0, controls: list[ft.Control] | None = None
    ) -> None:
        rail = ft.NavigationRail(
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            selected_index=selected_index,
            label_type=ft.NavigationRailLabelType.ALL,
            destinations=[
                ft.NavigationRailDestination(icon=ft.Icons.EVENT, label="Событие"),
                ft.NavigationRailDestination(icon=ft.Icons.PERSON_ADD, label="Гости"),
                ft.NavigationRailDestination(icon=ft.Icons.GROUP, label="Команда"),
                ft.NavigationRailDestination(icon=ft.Icons.ACCOUNT_BALANCE_WALLET, label="Бюджет"),
                ft.NavigationRailDestination(icon=ft.Icons.MAIL, label="Рассылка"),
            ],
            on_change=lambda e: self.go_to_section(int(e.control.selected_index)),
        )
        self.sidebar = ft.Container(
            ft.Column(
                [
                    ft.Container(rail, expand=True),
                    ft.Divider(),
                    ft.Button(
                        "Другие мероприятия",
                        icon=ft.Icons.SWAP_HORIZ,
                        on_click=lambda _: self.page.go("/"),
                    ),
                ],
                expand=True,
            ),
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            padding=10,
        )
        self.workspace_body = ft.Container(self.content, expand=True, padding=28)
        self.mobile_header = ft.Row([
            ft.TextButton("Все мероприятия", icon=ft.Icons.ARROW_BACK,
                          on_click=lambda _: self.page.go("/")),
        ])
        self.page.navigation_bar = ft.NavigationBar(
            selected_index=selected_index,
            destinations=[
                ft.NavigationBarDestination(icon=ft.Icons.EVENT, label="Событие"),
                ft.NavigationBarDestination(icon=ft.Icons.PERSON_ADD, label="Гости"),
                ft.NavigationBarDestination(icon=ft.Icons.GROUP, label="Команда"),
                ft.NavigationBarDestination(icon=ft.Icons.ACCOUNT_BALANCE_WALLET, label="Бюджет"),
                ft.NavigationBarDestination(icon=ft.Icons.MAIL, label="Рассылка"),
            ],
            on_change=lambda e: self.go_to_section(int(e.control.selected_index)),
        )
        self.page.clean()
        self.page.add(
            ft.Column([
                self.mobile_header,
                ft.Row([self.sidebar, self.workspace_body], expand=True, spacing=0),
            ], expand=True, spacing=0)
        )
        self.resize_workspace()
        if controls is None:
            self.show(selected_index)
        else:
            self.content.controls = controls
            self.page.update()

    def show_event_selector(self) -> None:
        self.data = None
        self.clear_workspace()
        self.page.clean()
        cards = []
        for event_data in self.store["events"]:
            event = event_data["event"]
            cards.append(
                ft.Card(
                    col={"xs": 12, "sm": 6, "md": 4, "lg": 3},
                    content=ft.Container(
                        ft.Column(
                            [
                                ft.Text(
                                    event["name"] or "Без названия",
                                    size=20,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    event["date"] or "Дата не указана",
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                                ft.Text(
                                    event["place"] or "Место не указано",
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                                ft.Row(
                                    [
                                        ft.Button(
                                            "Редактировать",
                                            icon=ft.Icons.EDIT,
                                            data=event_data["id"],
                                            on_click=self.select_event,
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.DELETE_OUTLINE,
                                            icon_color=ft.Colors.ERROR,
                                            tooltip="Удалить мероприятие",
                                            data=event_data["id"],
                                            on_click=self.open_delete_dialog,
                                        ),
                                    ]
                                ),
                            ]
                        ),
                        padding=18,
                    )
                )
            )
        empty = ft.Text(
            "Мероприятий пока нет. Добавьте первое.",
            italic=True,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        self.page.add(
            ft.Container(
                ft.Column(
                    [
                        ft.Text(
                            "Организатор мероприятий",
                            size=34,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text("Выберите мероприятие для редактирования"),
                        ft.Text("Мероприятия сохраняются в этом браузере на вашем устройстве."),
                        ft.Text("После редактирования в другой вкладке обновите эту страницу."),
                        ft.Button(
                            "Добавить мероприятие",
                            icon=ft.Icons.ADD,
                            on_click=lambda _: self.page.go("/events/add"),
                        ),
                        ft.Divider(),
                        ft.Row([
                            ft.Button("Экспорт JSON", icon=ft.Icons.DOWNLOAD, on_click=self.export_data),
                            ft.Button("Импорт JSON", icon=ft.Icons.UPLOAD, on_click=self.import_data),
                        ], wrap=True),
                        ft.ResponsiveRow(cards) if cards else empty,
                    ],
                    scroll=ft.ScrollMode.AUTO,
                ),
                padding=16,
                expand=True,
            )
        )

    def get_backup_picker(self):
        if self.backup_picker is None:
            self.backup_picker = ft.FilePicker()
            self.page.services.append(self.backup_picker)
        return self.backup_picker

    async def export_data(self, _):
        try:
            async with self._action_lock:
                data = await self.storage.load()
                self.store = data
            await self.get_backup_picker().save_file(
                file_name="event_data.json", src_bytes=encode_data(data).encode("utf-8"),
            )
        except Exception as error:
            self.notice(f"Не удалось экспортировать данные: {error}", True)

    async def import_data(self, _):
        try:
            files = await self.get_backup_picker().pick_files(
                allow_multiple=False, with_data=True, file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["json"],
            )
            if not files:
                return
            selected = files[0]
            if selected.size > MAX_DATA_BYTES or selected.bytes is None:
                raise ValueError("Выберите JSON-файл размером до 2 МБ")
            imported = decode_data(selected.bytes.decode("utf-8-sig"))
            async with self._action_lock:
                self.store = await self.storage.load()
                # Import as copies, so an archive cannot overwrite existing events.
                for item in imported["events"]:
                    item["id"] = uuid4().hex
                    self.store["events"].append(item)
                if await self.save_store():
                    self.page.go("/")
                    await self.route_change(None)
                    self.notice(f"Импортировано мероприятий: {len(imported['events'])}")
        except Exception as error:
            self.notice(f"Не удалось импортировать данные: {error}", True)

    def select_event(self, event) -> None:
        event_id = event.control.data
        self.page.go(f"/event/{event_id}")

    def show_add_event_page(self) -> None:
        self.data = None
        self.clear_workspace()
        self.page.clean()
        name = ft.TextField(
            label="Название", autofocus=True, col={"xs": 12, "md": 8}
        )
        date = ft.TextField(
            label="Дата (ДД.ММ.ГГГГ)", col={"xs": 12, "md": 4}
        )
        place = ft.TextField(
            label="Место проведения", col={"xs": 12}
        )
        description = ft.TextField(
            label="Описание", multiline=True, min_lines=2, col={"xs": 12}
        )

        async def create(_):
            try:
                if not name.value.strip():
                    raise ValueError("Укажите название мероприятия")
                datetime.strptime(date.value.strip(), "%d.%m.%Y")
                event_data = deepcopy(DEFAULT_EVENT)
                event_data["id"] = uuid4().hex
                event_data["event"] = {
                    "name": name.value.strip(),
                    "date": date.value.strip(),
                    "place": place.value.strip(),
                    "description": description.value.strip(),
                }
                self.store["events"].append(event_data)
                if not await self.save_store():
                    return
                self.page.go(f"/event/{event_data['id']}")
                self.notice("Мероприятие добавлено")
            except ValueError as error:
                message = (
                    "Дата должна быть в формате ДД.ММ.ГГГГ"
                    if "time data" in str(error)
                    else str(error)
                )
                self.notice(message, True)

        self.page.add(
            ft.Container(
                ft.Column(
                    self.header(
                        "Добавить мероприятие",
                        "Заполните основную информацию",
                    )
                    + [
                        ft.ResponsiveRow(
                            [
                                name,
                                date,
                                place,
                                description,
                            ],
                            spacing=16,
                            run_spacing=20,
                        ),
                        ft.Row(
                            [
                                ft.Button(
                                    "Назад",
                                    icon=ft.Icons.ARROW_BACK,
                                    on_click=lambda _: self.page.go("/"),
                                ),
                                ft.Button(
                                    "Добавить",
                                    icon=ft.Icons.ADD,
                                    on_click=self.action(create),
                                ),
                            ]
                        ),
                    ],
                    width=900,
                    spacing=20,
                    scroll=ft.ScrollMode.AUTO,
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                padding=32,
                expand=True,
                alignment=ft.Alignment.CENTER,
            )
        )

    def open_delete_dialog(self, event) -> None:
        event_id = event.control.data
        event_data = next(
            (
                item
                for item in self.store["events"]
                if item["id"] == event_id
            ),
            None,
        )
        if event_data is None:
            self.notice("Мероприятие уже удалено", True)
            return

        async def delete(_):
            self.store["events"] = [
                item
                for item in self.store["events"]
                if item["id"] != event_id
            ]
            if not await self.save_store():
                return
            self.page.pop_dialog()
            self.show_event_selector()
            self.notice("Мероприятие удалено")

        name = event_data["event"]["name"] or "Без названия"
        self.page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text("Удалить мероприятие?"),
                content=ft.Text(
                    f"«{name}» и все связанные гости, участники и статьи "
                    "бюджета будут удалены без возможности восстановления."
                ),
                actions=[
                    ft.Button(
                        "Отмена", on_click=lambda _: self.page.pop_dialog()
                    ),
                    ft.Button(
                        "Удалить",
                        icon=ft.Icons.DELETE,
                        color=ft.Colors.ERROR,
                        on_click=self.action(delete),
                    ),
                ],
            )
        )

    def show(self, index: int) -> None:
        views = (self.event_view, self.guests_view, self.team_view, self.budget_view, self.mail_view)
        self.content.controls = views[index]()
        self.page.update()

    def notice(self, message: str, error: bool = False) -> None:
        self.page.show_dialog(
            ft.SnackBar(
                ft.Text(
                    message,
                    color=ft.Colors.ON_ERROR_CONTAINER if error else ft.Colors.ON_TERTIARY_CONTAINER,
                ),
                bgcolor=ft.Colors.ERROR_CONTAINER if error else ft.Colors.TERTIARY_CONTAINER,
            )
        )

    @staticmethod
    def header(title: str, subtitle: str) -> list[ft.Control]:
        return [ft.Text(title, size=30, weight=ft.FontWeight.BOLD), ft.Text(subtitle, color=ft.Colors.ON_SURFACE_VARIANT), ft.Divider()]

    def event_view(self) -> list[ft.Control]:
        item = self.data["event"]
        name = ft.TextField(label="Название", value=item["name"], col={"xs": 12, "md": 8})
        date = ft.TextField(label="Дата (ДД.ММ.ГГГГ)", value=item["date"], col={"xs": 12, "md": 4})
        place = ft.TextField(label="Место проведения", value=item["place"])
        description = ft.TextField(label="Описание", value=item["description"], multiline=True, min_lines=3)

        async def save(_):
            try:
                if not name.value.strip():
                    raise ValueError("Укажите название")
                datetime.strptime(date.value.strip(), "%d.%m.%Y")
                self.data["event"] = {"name": name.value.strip(), "date": date.value.strip(), "place": place.value.strip(), "description": description.value.strip()}
                if not await self.save_store():
                    return
                self.notice("Мероприятие сохранено")
            except ValueError as error:
                self.notice("Проверьте название и дату в формате ДД.ММ.ГГГГ" if "time data" in str(error) else str(error), True)

        form = ft.Column(
            [
                ft.ResponsiveRow(
                    [name, date, place, description],
                    spacing=16,
                    run_spacing=20,
                ),
                ft.Row([ft.Button("Сохранить", icon=ft.Icons.SAVE, on_click=self.action(save))]),
            ],
            width=900,
            spacing=20,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        return self.header("Мероприятие", "Основная информация для писем") + [form]

    def person_add_view(
        self, target: str, role_required: bool
    ) -> list[ft.Control]:
        name = ft.TextField(label="ФИО", autofocus=True, col={"xs": 12, "md": 6})
        email = ft.TextField(label="Email", col={"xs": 12, "md": 6})
        role = (
            ft.TextField(label="Роль", col={"xs": 12, "md": 6})
            if role_required
            else None
        )
        section = "team" if role_required else "guests"
        success_message = (
            "Участник добавлен" if role_required else "Гость добавлен"
        )

        async def add(_):
            try:
                if len(name.value.strip().split()) < 2:
                    raise ValueError("Введите имя и фамилию")
                address = normalize_email(email.value)
                if any(x["email"].lower() == address.lower() for x in self.data[target]):
                    raise ValueError("Этот email уже добавлен")
                row = {"id": uuid4().hex, "name": name.value.strip(), "email": address}
                if role_required:
                    if not role.value.strip():
                        raise ValueError("Укажите роль")
                    row["role"] = role.value.strip()
                self.data[target].append(row)
                if not await self.save_store():
                    return
                self.page.go(self.event_route(section))
                self.notice(success_message)
            except ValueError as error:
                self.notice(str(error), True)

        controls = [name, email] + ([role] if role else [])
        title = "Добавить участника" if role_required else "Добавить гостя"
        subtitle = (
            "Укажите контакты и роль в команде"
            if role_required
            else "Укажите имя и email получателя приглашения"
        )
        form = ft.Column(
            [
                ft.ResponsiveRow(controls, spacing=16, run_spacing=20),
                ft.Row(
                    [
                        ft.Button(
                            "Назад",
                            icon=ft.Icons.ARROW_BACK,
                            on_click=lambda _: self.page.go(
                                self.event_route(section)
                            ),
                        ),
                        ft.Button("Добавить", icon=ft.Icons.ADD, on_click=self.action(add)),
                    ]
                ),
            ],
            width=900,
            spacing=20,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        return self.header(title, subtitle) + [form]

    def people_table(self, target: str, role_required: bool) -> ft.Control:
        if not self.data[target]:
            return ft.Text("Список пока пуст", italic=True, color=ft.Colors.ON_SURFACE_VARIANT)
        columns = [ft.DataColumn(ft.Text("ФИО")), ft.DataColumn(ft.Text("Email"))]
        if role_required:
            columns.append(ft.DataColumn(ft.Text("Роль")))
        columns.append(ft.DataColumn(ft.Text("")))
        rows = []
        for person in self.data[target]:
            cells = [ft.DataCell(ft.Text(person["name"])), ft.DataCell(ft.Text(person["email"]))]
            if role_required:
                cells.append(ft.DataCell(ft.Text(person["role"])))
            cells.append(ft.DataCell(ft.IconButton(ft.Icons.DELETE_OUTLINE, data=(target, person["id"], 2 if role_required else 1), on_click=self.action(self.delete_person))))
            rows.append(ft.DataRow(cells=cells))
        return ft.Row([ft.DataTable(columns=columns, rows=rows)], scroll=ft.ScrollMode.AUTO)

    async def delete_person(self, event) -> None:
        target, person_id, section = event.control.data
        self.data[target] = [p for p in self.data[target] if p["id"] != person_id]
        if not await self.save_store():
            return
        self.show(section)

    def people_section(self, target: str, role_required: bool) -> ft.ExpansionTile:
        title = "Список участников" if role_required else "Список гостей"
        return ft.ExpansionTile(
            title=ft.Text(f"{title} ({len(self.data[target])})"),
            leading=ft.Icons.GROUP,
            controls=[self.people_table(target, role_required)],
            controls_padding=16,
            expanded_cross_axis_alignment=ft.CrossAxisAlignment.STRETCH,
            expanded=False,
            maintain_state=True,
        )

    def guests_view(self) -> list[ft.Control]:
        return self.header("Гости", "Получатели приглашений") + [
            ft.Button(
                "Добавить гостя",
                icon=ft.Icons.PERSON_ADD,
                on_click=lambda _: self.page.go(
                    self.event_route("guests/add")
                ),
            ),
            self.people_section("guests", False),
        ]

    def team_view(self) -> list[ft.Control]:
        return self.header("Команда", "Участники и распределённые роли") + [
            ft.Button(
                "Добавить участника",
                icon=ft.Icons.PERSON_ADD,
                on_click=lambda _: self.page.go(self.event_route("team/add")),
            ),
            self.people_section("participants", True),
        ]

    def budget_view(self) -> list[ft.Control]:
        income, expenses, balance = budget_totals(self.data["budget"])
        cards = ft.Row(
            [
                self.metric("Доходы", income, ft.Colors.TERTIARY_CONTAINER, ft.Colors.ON_TERTIARY_CONTAINER),
                self.metric("Расходы", expenses, ft.Colors.ERROR_CONTAINER, ft.Colors.ON_ERROR_CONTAINER),
                self.metric(
                    "Дефицит" if balance < 0 else "Остаток",
                    abs(balance),
                    ft.Colors.ERROR_CONTAINER if balance < 0 else ft.Colors.TERTIARY_CONTAINER,
                    ft.Colors.ON_ERROR_CONTAINER if balance < 0 else ft.Colors.ON_TERTIARY_CONTAINER,
                ),
            ],
            wrap=True,
        )
        rows = []
        for key, label in (("income", "Доход"), ("expenses", "Расход")):
            for row in self.data["budget"][key]:
                rows.append(ft.DataRow(cells=[ft.DataCell(ft.Text(label)), ft.DataCell(ft.Text(row["title"])), ft.DataCell(ft.Text(f"{row['amount']:,.2f} ₽")), ft.DataCell(ft.IconButton(ft.Icons.DELETE_OUTLINE, data=(key, row["id"]), on_click=self.action(self.delete_budget)))]))
        table = ft.DataTable(columns=[ft.DataColumn(ft.Text("Тип")), ft.DataColumn(ft.Text("Статья")), ft.DataColumn(ft.Text("Сумма")), ft.DataColumn(ft.Text(""))], rows=rows) if rows else ft.Text("Статей пока нет", italic=True)
        return self.header("Бюджет", "Автоматический контроль дефицита") + [
            cards,
            ft.Button(
                "Добавить статью",
                icon=ft.Icons.ADD,
                on_click=lambda _: self.page.go(
                    self.event_route("budget/add")
                ),
            ),
            ft.Row([table], scroll=ft.ScrollMode.AUTO),
        ]

    def budget_add_view(self) -> list[ft.Control]:
        title = ft.TextField(
            label="Статья", autofocus=True, col={"xs": 12, "md": 6}
        )
        amount = ft.TextField(
            label="Сумма, ₽",
            keyboard_type=ft.KeyboardType.NUMBER,
            col={"xs": 12, "md": 3},
        )
        kind = ft.Dropdown(
            label="Тип",
            value="expenses",
            col={"xs": 12, "md": 3},
            options=[
                ft.DropdownOption(key="income", text="Доход"),
                ft.DropdownOption(key="expenses", text="Расход"),
            ],
        )

        async def add(_):
            try:
                if not title.value.strip():
                    raise ValueError("Введите название статьи")
                self.data["budget"][kind.value].append(
                    {
                        "id": uuid4().hex,
                        "title": title.value.strip(),
                        "amount": parse_amount(amount.value),
                    }
                )
                if not await self.save_store():
                    return
                self.page.go(self.event_route("budget"))
                self.notice("Статья бюджета добавлена")
            except ValueError as error:
                self.notice(str(error), True)

        form = ft.Column(
            [
                ft.ResponsiveRow(
                    [title, amount, kind], spacing=16, run_spacing=20
                ),
                ft.Row(
                    [
                        ft.Button(
                            "Назад",
                            icon=ft.Icons.ARROW_BACK,
                            on_click=lambda _: self.page.go(
                                self.event_route("budget")
                            ),
                        ),
                        ft.Button("Добавить", icon=ft.Icons.ADD, on_click=self.action(add)),
                    ]
                ),
            ],
            width=900,
            spacing=20,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        return self.header(
            "Добавить статью бюджета",
            "Укажите тип, название и сумму",
        ) + [form]

    @staticmethod
    def metric(label: str, value: float, color, text_color) -> ft.Control:
        return ft.Container(ft.Column([ft.Text(label, color=text_color), ft.Text(f"{value:,.2f} ₽", size=23, weight=ft.FontWeight.BOLD, color=text_color)]), bgcolor=color, padding=18, border_radius=12, width=230)

    async def delete_budget(self, event) -> None:
        key, row_id = event.control.data
        self.data["budget"][key] = [x for x in self.data["budget"][key] if x["id"] != row_id]
        if not await self.save_store():
            return
        self.show(3)

    def mail_view(self) -> list[ft.Control]:
        settings = mail_settings(self.data["mail"])
        provider = ft.Dropdown(
            label="Почтовый сервис", value=settings["provider"],
            options=[ft.DropdownOption(key=key, text=item["name"]) for key, item in MAIL_PROVIDERS.items()],
            col={"xs": 12, "md": 4},
        )
        sender = ft.TextField(label="Email отправителя", value=settings["sender"], col={"xs": 12, "md": 8})
        password = ft.TextField(label="Пароль приложения", value=self.password, password=True, can_reveal_password=True)
        script = ft.TextField(label="Файл сценария", value=self.script_attachment.name if self.script_attachment else "", read_only=True)
        if self.mail_picker is None:
            self.mail_picker = ft.FilePicker()
            self.page.services.append(self.mail_picker)

        editors = {}

        def current_template(target):
            subject, body = editors[target]
            return {"subject": subject.value, "body": body.value}

        def preview_person(target):
            return next(iter(self.data[target]), {"name": "Иван Иванов", "email": "ivan@example.com", "role": "Ведущий"})

        async def persist():
            templates = {target: current_template(target) for target in editors}
            for target, template in templates.items():
                render_mail(template, preview_person(target), self.data["event"])
            self.data["mail"] = {
                "sender": sender.value.strip(), "provider": provider.value,
                "templates": templates,
            }
            if not await self.save_store():
                return False
            self.password = password.value
            return True

        async def save(_):
            try:
                if await persist():
                    self.notice("Настройки и тексты писем сохранены")
            except (ValueError, OSError) as error:
                self.notice(str(error), True)

        def preview(target):
            try:
                subject, body = render_mail(current_template(target), preview_person(target), self.data["event"])
                self.page.show_dialog(ft.AlertDialog(
                    title=ft.Text("Предпросмотр письма"),
                    content=ft.Column([
                        ft.Text("Данные первого получателя; если список пуст — пример."),
                        ft.Text(f"Тема: {subject}", weight=ft.FontWeight.BOLD, selectable=True),
                        ft.Text(body, selectable=True),
                        ft.Text("Вложение: " + (script.value or "файл не выбран")) if target == "participants" else ft.Text("Без вложений"),
                    ], width=650, tight=True, scroll=ft.ScrollMode.AUTO),
                    actions=[ft.Button("Закрыть", on_click=lambda _: self.page.pop_dialog())],
                ))
            except ValueError as error:
                self.notice(str(error), True)

        def reset(target):
            subject, body = editors[target]
            subject.value = DEFAULT_TEMPLATES[target]["subject"]
            body.value = DEFAULT_TEMPLATES[target]["body"]
            self.page.update()

        template_sections = []
        for target, title in (("guests", "Письмо гостям"), ("participants", "Письмо команде")):
            template = settings["templates"][target]
            subject = ft.TextField(label="Тема письма", value=template["subject"])
            body = ft.TextField(label="Текст письма", value=template["body"], multiline=True, min_lines=6, max_lines=16)
            editors[target] = (subject, body)
            template_sections.append(ft.ExpansionTile(
                title=ft.Text(title), leading=ft.Icons.EDIT,
                expanded=False, maintain_state=True, controls_padding=16,
                expanded_cross_axis_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[ft.Column([
                    subject, body,
                    ft.Text("Подстановки: {name} — ФИО, {email} — email, {event_name} — мероприятие, {date} — дата, {place} — место, {description} — описание, {role} — роль в команде. Для обычных фигурных скобок используйте {{ и }}."),
                    ft.Row([
                        ft.Button("Предпросмотр", icon=ft.Icons.VISIBILITY, on_click=lambda _, t=target: preview(t)),
                        ft.Button("Стандартный текст", on_click=lambda _, t=target: reset(t)),
                    ], wrap=True),
                ], spacing=12)],
            ))

        help_text = ft.Markdown(MAIL_HELP[provider.value], selectable=True, auto_follow_links=True)

        def provider_changed(_):
            help_text.value = MAIL_HELP[provider.value]
            password.value = ""
            self.password = ""
            self.page.update()

        provider.on_select = provider_changed

        async def choose_file(_):
            event_id = self.data["id"]
            try:
                files = await self.mail_picker.pick_files(
                    dialog_title="Выберите сценарий", allow_multiple=False, with_data=True,
                )
                if not files or self.data is None or self.data["id"] != event_id:
                    return
                selected = files[0]
                if selected.size > MAX_SCRIPT_BYTES:
                    raise ValueError("Размер сценария не должен превышать 5 МБ")
                self.script_attachment = ScriptAttachment(selected.name, selected.bytes)
                script.value = self.script_attachment.name
                self.page.update()
            except Exception as error:
                self.notice(f"Не удалось выбрать сценарий: {error}", True)

        async def send(target: str):
            if self.sending:
                return
            self.sending = True
            try:
                async with self._action_lock:
                    people = deepcopy(self.data[target])
                    if not people:
                        raise ValueError("Список получателей пуст")
                    attachment = self.script_attachment
                    if target == "participants" and attachment is None:
                        raise ValueError("Выберите файл сценария")
                    event_data = deepcopy(self.data["event"])
                    template = current_template(target)
                    credentials = (sender.value, password.value, provider.value)
                    if not await persist():
                        return

                def deliver():
                    if target == "guests":
                        return MailService(*credentials).send_invitations(people, event_data, template)
                    return MailService(*credentials).send_participant_notices(people, event_data, attachment, template)

                self.sending = True
                for button in self.send_buttons:
                    button.disabled = True
                self.page.update()
                count = await asyncio.to_thread(deliver)
                self.notice(f"Отправлено писем: {count}")
            except Exception as error:
                self.notice(f"Ошибка рассылки: {error}", True)
            finally:
                self.sending = False
                for button in self.send_buttons:
                    button.disabled = False
                self.page.update()

        async def send_guests(_):
            await send("guests")

        async def send_team(_):
            await send("participants")

        self.send_buttons = [
            ft.Button("Пригласить гостей", icon=ft.Icons.SEND, on_click=send_guests, disabled=self.sending),
            ft.Button("Уведомить команду", icon=ft.Icons.ATTACH_EMAIL, on_click=send_team, disabled=self.sending),
        ]

        return self.header("Рассылка", "Тексты сохраняются отдельно для каждого мероприятия. Пароль приложения не сохраняется на диске.") + [
            ft.ResponsiveRow([sender, provider]), password,
            ft.ExpansionTile(
                title=ft.Text("Инструкция для выбранного почтового сервиса"),
                leading=ft.Icons.HELP_OUTLINE, expanded=False, maintain_state=True,
                controls=[help_text], controls_padding=16,
                expanded_cross_axis_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            *template_sections,
            ft.Button("Сохранить настройки и тексты", icon=ft.Icons.SAVE, on_click=self.action(save)),
            ft.Text("Перед выходом из раздела сохраните изменения. При отправке тексты сохраняются автоматически."),
            ft.Divider(),
            ft.Text("Отправка", size=22, weight=ft.FontWeight.BOLD),
            ft.Text("Сценарий до 5 МБ прикладывается только к письмам команды. После обновления страницы выберите файл заново."),
            script,
            ft.Button("Выбрать файл", icon=ft.Icons.FOLDER_OPEN, on_click=choose_file),
            ft.Row(self.send_buttons, wrap=True),
        ]
