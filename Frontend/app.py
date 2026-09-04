from datetime import datetime
from uuid import uuid4

import flet as ft

from Backend.services import MailService, budget_totals, normalize_email, parse_amount
from Backend.storage import JsonStorage


class EventPlannerApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.storage = JsonStorage()
        self.data = self.storage.load()
        self.content = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO)
        self.password = ""
        self.script_path = ""

    def build(self) -> None:
        self.page.title = "Организатор мероприятий"
        self.page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
        self.page.padding = 0
        rail = ft.NavigationRail(
            selected_index=0,
            label_type=ft.NavigationRailLabelType.ALL,
            destinations=[
                ft.NavigationRailDestination(icon=ft.Icons.EVENT, label="Событие"),
                ft.NavigationRailDestination(icon=ft.Icons.PERSON_ADD, label="Гости"),
                ft.NavigationRailDestination(icon=ft.Icons.GROUP, label="Команда"),
                ft.NavigationRailDestination(icon=ft.Icons.ACCOUNT_BALANCE_WALLET, label="Бюджет"),
                ft.NavigationRailDestination(icon=ft.Icons.MAIL, label="Рассылка"),
            ],
            on_change=lambda e: self.show(int(e.control.selected_index)),
        )
        self.page.add(ft.Row([ft.Container(rail, bgcolor=ft.Colors.INDIGO_50, padding=10), ft.VerticalDivider(width=1), ft.Container(self.content, expand=True, padding=28)], expand=True, spacing=0))
        self.show(0)

    def show(self, index: int) -> None:
        views = (self.event_view, self.guests_view, self.team_view, self.budget_view, self.mail_view)
        self.content.controls = views[index]()
        self.page.update()

    def notice(self, message: str, error: bool = False) -> None:
        self.page.show_dialog(ft.SnackBar(ft.Text(message), bgcolor=ft.Colors.RED_700 if error else ft.Colors.GREEN_700))

    @staticmethod
    def header(title: str, subtitle: str) -> list[ft.Control]:
        return [ft.Text(title, size=30, weight=ft.FontWeight.BOLD), ft.Text(subtitle, color=ft.Colors.GREY_700), ft.Divider()]

    def event_view(self) -> list[ft.Control]:
        item = self.data["event"]
        name = ft.TextField(label="Название", value=item["name"], expand=True)
        date = ft.TextField(label="Дата (ДД.ММ.ГГГГ)", value=item["date"], width=230)
        place = ft.TextField(label="Место проведения", value=item["place"])
        description = ft.TextField(label="Описание", value=item["description"], multiline=True, min_lines=3)

        def save(_):
            try:
                if not name.value.strip():
                    raise ValueError("Укажите название")
                datetime.strptime(date.value.strip(), "%d.%m.%Y")
                self.data["event"] = {"name": name.value.strip(), "date": date.value.strip(), "place": place.value.strip(), "description": description.value.strip()}
                self.storage.save(self.data)
                self.notice("Мероприятие сохранено")
            except ValueError as error:
                self.notice("Проверьте название и дату в формате ДД.ММ.ГГГГ" if "time data" in str(error) else str(error), True)

        return self.header("Мероприятие", "Основная информация для писем") + [ft.Row([name, date]), place, description, ft.Button("Сохранить", icon=ft.Icons.SAVE, on_click=save)]

    def person_form(self, target: str, role_required: bool) -> ft.Control:
        name = ft.TextField(label="ФИО", expand=True)
        email = ft.TextField(label="Email", expand=True)
        role = ft.TextField(label="Роль", expand=True) if role_required else None

        def add(_):
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
                self.storage.save(self.data)
                self.show(2 if role_required else 1)
            except ValueError as error:
                self.notice(str(error), True)

        controls = [name, email] + ([role] if role else [])
        return ft.Row(controls + [ft.IconButton(ft.Icons.ADD_CIRCLE, on_click=add)])

    def people_table(self, target: str, role_required: bool) -> ft.Control:
        if not self.data[target]:
            return ft.Text("Список пока пуст", italic=True, color=ft.Colors.GREY)
        columns = [ft.DataColumn(ft.Text("ФИО")), ft.DataColumn(ft.Text("Email"))]
        if role_required:
            columns.append(ft.DataColumn(ft.Text("Роль")))
        columns.append(ft.DataColumn(ft.Text("")))
        rows = []
        for person in self.data[target]:
            cells = [ft.DataCell(ft.Text(person["name"])), ft.DataCell(ft.Text(person["email"]))]
            if role_required:
                cells.append(ft.DataCell(ft.Text(person["role"])))
            cells.append(ft.DataCell(ft.IconButton(ft.Icons.DELETE_OUTLINE, data=person["id"], on_click=lambda e, t=target, i=2 if role_required else 1: self.delete_person(t, e.control.data, i))))
            rows.append(ft.DataRow(cells=cells))
        return ft.Row([ft.DataTable(columns=columns, rows=rows)], scroll=ft.ScrollMode.AUTO)

    def delete_person(self, target: str, person_id: str, section: int) -> None:
        self.data[target] = [p for p in self.data[target] if p["id"] != person_id]
        self.storage.save(self.data)
        self.show(section)

    def guests_view(self) -> list[ft.Control]:
        return self.header("Гости", "Получатели приглашений") + [self.person_form("guests", False), self.people_table("guests", False)]

    def team_view(self) -> list[ft.Control]:
        return self.header("Команда", "Участники и распределённые роли") + [self.person_form("participants", True), self.people_table("participants", True)]

    def budget_view(self) -> list[ft.Control]:
        title = ft.TextField(label="Статья", expand=True)
        amount = ft.TextField(label="Сумма, ₽", width=170, keyboard_type=ft.KeyboardType.NUMBER)
        kind = ft.Dropdown(label="Тип", value="expenses", width=170, options=[ft.DropdownOption(key="income", text="Доход"), ft.DropdownOption(key="expenses", text="Расход")])

        def add(_):
            try:
                if not title.value.strip():
                    raise ValueError("Введите название статьи")
                self.data["budget"][kind.value].append({"id": uuid4().hex, "title": title.value.strip(), "amount": parse_amount(amount.value)})
                self.storage.save(self.data)
                self.show(3)
            except ValueError as error:
                self.notice(str(error), True)

        income, expenses, balance = budget_totals(self.data["budget"])
        cards = ft.Row([self.metric("Доходы", income, ft.Colors.GREEN_50), self.metric("Расходы", expenses, ft.Colors.RED_50), self.metric("Дефицит" if balance < 0 else "Остаток", abs(balance), ft.Colors.RED_100 if balance < 0 else ft.Colors.BLUE_50, ft.Colors.RED_700 if balance < 0 else ft.Colors.GREEN_700)], wrap=True)
        rows = []
        for key, label in (("income", "Доход"), ("expenses", "Расход")):
            for row in self.data["budget"][key]:
                rows.append(ft.DataRow(cells=[ft.DataCell(ft.Text(label)), ft.DataCell(ft.Text(row["title"])), ft.DataCell(ft.Text(f"{row['amount']:,.2f} ₽")), ft.DataCell(ft.IconButton(ft.Icons.DELETE_OUTLINE, data=(key, row["id"]), on_click=self.delete_budget))]))
        table = ft.DataTable(columns=[ft.DataColumn(ft.Text("Тип")), ft.DataColumn(ft.Text("Статья")), ft.DataColumn(ft.Text("Сумма")), ft.DataColumn(ft.Text(""))], rows=rows) if rows else ft.Text("Статей пока нет", italic=True)
        return self.header("Бюджет", "Автоматический контроль дефицита") + [cards, ft.Row([title, amount, kind, ft.IconButton(ft.Icons.ADD_CIRCLE, on_click=add)]), ft.Row([table], scroll=ft.ScrollMode.AUTO)]

    @staticmethod
    def metric(label: str, value: float, color, text_color=None) -> ft.Control:
        return ft.Container(ft.Column([ft.Text(label), ft.Text(f"{value:,.2f} ₽", size=23, weight=ft.FontWeight.BOLD, color=text_color)]), bgcolor=color, padding=18, border_radius=12, width=230)

    def delete_budget(self, event) -> None:
        key, row_id = event.control.data
        self.data["budget"][key] = [x for x in self.data["budget"][key] if x["id"] != row_id]
        self.storage.save(self.data)
        self.show(3)

    def mail_view(self) -> list[ft.Control]:
        sender = ft.TextField(label="Email отправителя (Gmail)", value=self.data["mail"].get("sender", ""), expand=True)
        password = ft.TextField(label="Пароль приложения", password=True, can_reveal_password=True, expand=True)
        script = ft.TextField(label="Файл сценария", value=self.script_path, read_only=True, expand=True)
        picker = ft.FilePicker()
        self.page.services.append(picker)

        async def choose_file(_):
            files = await picker.pick_files(dialog_title="Выберите сценарий", allow_multiple=False)
            if files:
                self.script_path = files[0].path
                script.value = self.script_path
                self.page.update()

        def remember():
            self.data["mail"]["sender"] = sender.value.strip()
            self.password = password.value
            self.script_path = script.value.strip()
            self.storage.save(self.data)

        def send(target: str):
            remember()
            try:
                people = self.data[target]
                if not people:
                    raise ValueError("Список получателей пуст")
                service = MailService(sender.value, self.password)
                count = service.send_invitations(people, self.data["event"]) if target == "guests" else service.send_participant_notices(people, self.data["event"], self.script_path)
                self.notice(f"Отправлено писем: {count}")
            except Exception as error:
                self.notice(f"Ошибка рассылки: {error}", True)

        return self.header("Рассылка", "Пароль приложения не сохраняется на диске") + [ft.Row([sender, password]), ft.Row([script, ft.Button("Выбрать файл", icon=ft.Icons.FOLDER_OPEN, on_click=choose_file)]), ft.Row([ft.Button("Пригласить гостей", icon=ft.Icons.SEND, on_click=lambda _: send("guests")), ft.Button("Уведомить команду", icon=ft.Icons.ATTACH_EMAIL, on_click=lambda _: send("participants"))], wrap=True)]

