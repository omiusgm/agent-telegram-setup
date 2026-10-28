"""Build editable teaching SVGs and the step index. No accounts or network."""
from html import escape
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'docs/assets/dot-guide'
W, H = 1200, 800
NAVY, INK, MUTED = '#183047', '#213b51', '#5f7385'
BLUE, GREEN, AMBER, RED = '#167fc5', '#247d5e', '#8a650b', '#ad4d43'


class Picture:
    def __init__(self, number, title, subtitle):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
            '<title>' + escape(title) + '</title>',
            '<desc>Учебная иллюстрация. Условные данные. Не снимок личного аккаунта и не доказательство подключения.</desc>',
            '<style>text{font-family:Arial,"DejaVu Sans",sans-serif} .mono{font-family:Menlo,"DejaVu Sans Mono",monospace}</style>']
        self.box(0, 0, W, H, '#eef3f7', radius=0)
        self.box(24, 24, 1152, 752, '#ffffff', radius=22)
        self.box(24, 24, 1152, 110, NAVY, radius=22)
        self.box(24, 105, 1152, 29, NAVY, radius=0)
        self.text(52, 56, 'АГЕНТ ЁЖ  /  TELEGRAM MCP → DOT', 14, '#b5d0e4', bold=True)
        self.text(52, 96, title, 30, '#ffffff', bold=True)
        self.text(52, 165, subtitle, 19, MUTED)
        self.pill(1038, 44, 'СХЕМА' if number == 0 else f'ШАГ {number}', '#34556f', '#ffffff', width=108)
        self.line(52, 722, 1148, 722, '#dce6ed')
        self.text(52, 751, 'Учебная иллюстрация • условные данные • не снимок вашего аккаунта', 17, MUTED)

    def box(self, x, y, w, h, fill='#f6f9fc', stroke='none', radius=14):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}"/>')

    def text(self, x, y, value, size=21, color=INK, bold=False, mono=False):
        weight = '700' if bold else '400'
        cls = ' class="mono"' if mono else ''
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}"{cls}>{escape(value)}</text>')

    def lines(self, x, y, values, size=21, color=INK, gap=31, bold=False):
        for i, value in enumerate(values):
            self.text(x, y + i * gap, value, size, color, bold)

    def pill(self, x, y, value, fill='#eaf4fc', color=BLUE, width=170):
        self.box(x, y, width, 34, fill, radius=17)
        self.text(x + 14, y + 23, value, 15, color, bold=True)

    def line(self, x1, y1, x2, y2, color='#c8d8e4', width=2):
        self.parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}"/>')

    def arrow(self, x1, y, x2):
        self.line(x1, y, x2, y, BLUE, 3)
        self.parts.append(f'<polyline points="{x2-8},{y-6} {x2},{y} {x2-8},{y+6}" fill="none" stroke="{BLUE}" stroke-width="3"/>')

    def field(self, x, y, label, value, width=680):
        self.text(x, y, label, 17, MUTED)
        self.box(x, y + 13, width, 52, '#ffffff', '#cddce7', 10)
        self.text(x + 16, y + 47, value, 21)

    def button(self, x, y, label, width=240, fill=BLUE):
        self.box(x, y, width, 48, fill, radius=10)
        self.text(x + 17, y + 31, label, 19, '#ffffff', bold=True)

    def note(self, x, y, w, lines, fill='#edf6fc', color=BLUE):
        self.box(x, y, w, 28 + len(lines) * 28, fill)
        self.lines(x + 18, y + 29, lines, 18, color, gap=28)

    def save(self, stem):
        ASSETS.mkdir(parents=True, exist_ok=True)
        (ASSETS / (stem + '.svg')).write_text('\n'.join(self.parts + ['</svg>']) + '\n', encoding='utf-8')


def overview():
    p = Picture(0, 'Один Telegram-сервис, прямой доступ Dot', 'Серверная сессия остаётся у владельца. Каждый этап проверяется отдельно.')
    nodes = [(52, 174, 'DOT', ['Личный агент']), (270, 174, 'ПЛАГИН', ['Установлен у вас']),
             (488, 224, 'ТУННЕЛЬ OPENAI', ['Защищённый маршрут']), (756, 174, 'MCP НА LINUX', ['Один backend']),
             (974, 174, 'TELEGRAM', ['Личный аккаунт'])]
    for i, (x, w, title, lines) in enumerate(nodes):
        p.box(x, 226, w, 136, '#edf5fb', '#c8deed')
        p.text(x + 15, 268, title, 17, BLUE, bold=True)
        p.lines(x + 15, 310, lines, 17, MUTED)
        if i < len(nodes)-1:
            p.arrow(x + w + 8, 294, nodes[i+1][0]-8)
    p.text(52, 416, 'Созданный туннель ещё не означает «Dot подключён»', 27, bold=True)
    gates = [('1', 'Telegram', 'Чтение проверено'), ('2', 'Туннель', 'Привязка верна'),
             ('3', 'Polling', 'Вход подтверждён'), ('4', 'Плагин', 'Установлен'), ('5', 'Вызов Dot', 'Получен ответ')]
    for i, (n, title, detail) in enumerate(gates):
        x = 52 + 222 * i
        p.box(x, 451, 208, 133, '#f6f9fc')
        p.pill(x+14, 465, n, width=43)
        p.text(x+14, 532, title, 22, bold=True)
        p.text(x+14, 560, detail, 15, MUTED)
    p.note(52, 619, 1096, ['Отправка включается отдельным правом. Первый тест записи — только собственное «Избранное».'])
    return p


def telegram():
    p = Picture(1, 'Локальный мастер: Telegram и права', 'Учебная схема Telegram-шага общего setup; не новый независимый интерфейс.')
    p.box(52, 198, 1096, 480, '#f6f9fc', '#dce7ef')
    p.box(52, 198, 1096, 44, '#e5edf4')
    p.text(74, 227, '127.0.0.1:PORT  •  приватный мастер владельца', 17, MUTED)
    p.text(78, 286, 'Что разрешить агенту?', 28, bold=True)
    p.box(78, 311, 28, 28, BLUE, radius=5); p.text(84, 332, '✓', 21, '#ffffff')
    p.text(122, 334, 'Читать разрешённые чаты и контакты', 23)
    p.box(78, 365, 28, 28, '#ffffff', '#a9bfce', 5)
    p.text(122, 388, 'Текст в «Избранное» — отдельный тест', 23)
    p.text(122, 419, 'Набор действий и область чатов выбираются независимо.', 18, MUTED)
    p.note(78, 450, 1044, ['Если сессия уже работает — проверить и использовать её.', 'Новый вход: QR из «Устройства» или телефон/код, затем 2FA.'], fill='#eaf7f0', color=GREEN)
    p.button(78, 574, 'Проверить Telegram', 258)
    p.text(364, 606, 'Ключи и пароль вводятся локально, не в чат.', 19, MUTED)
    return p


def tunnel():
    p = Picture(2, 'Platform: только своё личное окружение', 'Organization — окружение API. Workspace — среда ChatGPT, где работает ваш Dot.')
    p.box(52, 197, 758, 495, '#f7fafc', '#dce7ef')
    p.text(76, 232, 'Create tunnel', 24, bold=True)
    p.field(76, 267, 'Name', 'Мой Telegram', 710)
    p.field(76, 351, 'Description', 'Личный MCP, согласованные права', 710)
    p.field(76, 435, 'Organizations', 'Personal Organization — ваш личный вход', 710)
    p.field(76, 519, 'ChatGPT workspaces', 'Личный workspace — соответствие проверено', 710)
    p.button(566, 618, 'Create', 220)
    p.note(836, 226, 312, ['Старые и чужие команды', 'не выбирают наугад.', '', 'Если виден только ID,', 'агент проверяет вход', 'и соответствие среды.'])
    p.note(836, 470, 312, ['После создания:', 'TUNNEL_ID', '', 'Имя и описание', 'не выдают права MCP.'], fill='#fff6df', color=AMBER)
    return p


def credential():
    p = Picture(3, 'Runtime key: приватный ввод и проверка', 'Ключ control plane нужен tunnel-client. В чат и на иллюстрацию значение не попадает.')
    p.box(52, 210, 356, 444, '#f6f9fc', '#dce7ef')
    p.text(76, 249, 'Права runtime', 25, bold=True)
    p.pill(76, 280, 'TUNNELS', width=132)
    p.lines(76, 355, ['Read — требуется', 'Use — требуется'], 23, GREEN, gap=45)
    p.lines(76, 470, ['Manage — отдельная роль', 'для создания/изменения,', 'не обычного daemon.'], 19, MUTED)
    p.box(440, 210, 708, 444, '#f6f9fc', '#dce7ef')
    p.text(464, 249, 'Общий setup: раздел runtime', 25, bold=True)
    p.field(464, 282, 'Что подключаем', 'Telegram MCP', 300)
    p.field(788, 282, 'Tunnel ID', 'TUNNEL_ID', 336)
    p.field(464, 369, 'Runtime API key: приватный ввод владельцем', 'Значение здесь не изображается', 660)
    p.button(464, 463, 'Проверить и сохранить', 296)
    p.note(464, 535, 660, ['Сохраняется на компьютере мастера.', 'Metadata read не доказывает Use и polling.'], fill='#eaf7f0', color=GREEN)
    p.text(52, 695, 'Для runtime на Linux ключ и file reference должны находиться на согласованном Linux-хосте.', 20, MUTED)
    return p


def polling():
    p = Picture(4, 'Процесс жив ≠ polling авторизован', 'Пример критериев проверки. Названия полей зависят от версии tunnel-client.')
    p.box(52, 210, 524, 416, '#fff3ef', '#efd3cc')
    p.text(76, 254, 'Этого недостаточно', 27, RED, bold=True)
    p.lines(76, 310, ['Процесс запущен', 'Health / ready отвечают', 'Polling получает 401 или 403'], 23, INK, gap=51)
    p.pill(76, 506, 'ЭТАП НЕ ПРОЙДЕН', '#f8dcd5', RED, width=230)
    p.box(604, 210, 544, 416, '#ecf8f1', '#cae7d7')
    p.text(628, 254, 'Нужны подтверждения', 27, GREEN, bold=True)
    p.lines(628, 310, ['Свежий успешный authenticated poll', 'Правильный TUNNEL_ID', 'Нет текущих ошибок авторизации', 'MCP upstream доступен'], 21, INK, gap=51)
    p.pill(628, 550, 'МАРШРУТ ГОТОВ К ТЕСТУ', '#d1ebdc', GREEN, width=305)
    p.note(52, 646, 1096, ['Следующий этап — установка плагина и реальный вызов Dot. Они проверяются отдельно.'])
    return p


def plugin():
    p = Picture(5, 'ChatGPT Plugins: создать и установить', 'Личная интеграция • Connection: Tunnel • тот же идентификатор туннеля.')
    p.box(52, 198, 748, 493, '#f7fafc', '#dce7ef')
    p.text(76, 235, 'Add custom MCP server', 24, bold=True)
    p.field(76, 274, 'Name', 'Мой Telegram', 700)
    p.field(76, 358, 'Connection', 'Tunnel  →  TUNNEL_ID', 700)
    p.field(76, 442, 'Authentication для нашего stdio upstream', 'No authentication *', 700)
    p.button(76, 555, 'Create as a plugin', 276)
    p.arrow(367, 579, 422)
    p.button(438, 555, 'Установить (+)', 238, GREEN)
    p.note(827, 228, 321, ['* Только для нашего', 'private stdio через', 'защищённый туннель.', '', 'Вход runtime в OpenAI', 'остаётся обязательным.'])
    p.note(827, 475, 321, ['Это не инструкция', 'отключить авторизацию', 'публичного HTTP MCP.'], fill='#fff6df', color=AMBER)
    p.text(76, 657, 'Созданная карточка ещё не означает установленный плагин.', 19, MUTED)
    return p


def dot_read():
    p = Picture(6, 'Проверка именно из вашего Dot', 'Учебный пример ответа. На вашей установке нужен собственный реальный вызов.')
    p.box(52, 204, 1096, 95, '#edf5fb')
    p.lines(76, 241, ['Используй плагин «Мой Telegram». Вызови telegram_status,', 'затем покажи до трёх разрешённых диалогов.'], 23)
    p.box(52, 329, 524, 323, '#f6f9fc', '#dce7ef')
    p.text(76, 372, 'Вызов MCP из Dot', 26, bold=True)
    p.text(76, 422, 'telegram_status', 23, BLUE, mono=True)
    p.text(76, 465, 'connected: true', 22, GREEN, mono=True)
    p.text(76, 505, 'read_only: true', 22, GREEN, mono=True)
    p.lines(76, 569, ['Это пример полей,', 'а не сохранённый результат.'], 19, MUTED)
    p.box(604, 329, 544, 323, '#ecf8f1', '#cae7d7')
    p.text(628, 372, 'Один выбранный чат', 26, bold=True)
    p.lines(628, 423, ['Владелец выбирает разрешённый диалог.', 'read_history_page: limit = 1', 'Dot получает одно сообщение.'], 21, gap=49)
    p.pill(628, 584, 'ПРИМЕР: READ ПОДТВЕРЖДЁН', '#d1ebdc', GREEN, width=345)
    p.text(52, 695, 'Локальный SDK и чтение через Codex за Dot этот этап не заменяют.', 20, MUTED)
    return p


def saved_text():
    p = Picture(7, 'Опционально: один текст в «Избранное»', 'Только по поручению и с подтверждением владельца. Другие получатели не открываются.')
    p.box(52, 207, 570, 332, '#f6f9fc', '#dce7ef')
    p.text(76, 250, 'send_saved_text', 24, BLUE, bold=True, mono=True)
    p.lines(76, 300, ['text: Тест подключения', 'request_id: dot-saved-demo-001', 'Получатель: собственное «Избранное»'], 20, gap=43)
    p.button(76, 455, 'Подтверждение владельца', 330)
    p.box(650, 207, 498, 332, '#ecf8f1', '#cae7d7')
    p.text(674, 250, 'Пример квитанции', 25, GREEN, bold=True)
    for i, row in enumerate(['destination: saved_messages', 'status: delivered_verified', 'readback_verified: true', 'message_id: 101  (пример)']):
        p.text(674, 302+i*47, row, 19, INK, mono=True)
    p.note(52, 573, 1096, ['uncertain_do_not_retry → сначала осмотр результата.', 'Не создавайте новый request_id ради повтора: сообщение уже могло отправиться.'], fill='#fff6df', color=AMBER)
    return p


STEPS = [
    ('00-route', 'overview', 'Общий маршрут', None, 'Независимые этапы; не результат подключения', overview),
    ('01-telegram', 'telegram_auth_or_scope', 'Telegram и права', 'step-1', 'Настоящее ограниченное чтение MCP', telegram),
    ('02-tunnel', 'tunnel_or_workspace_missing', 'Личный туннель', 'step-2', 'Объект и соответствие личной среде', tunnel),
    ('03-runtime-key', 'runtime_credential_needed', 'Приватный runtime key', 'step-3', 'Формат и опциональный metadata Read; Use и polling проверяются отдельно', credential),
    ('04-polling', 'polling_not_authenticated', 'Authenticated polling', 'step-4', 'Успешный свежий авторизованный poll и upstream', polling),
    ('05-plugin', 'plugin_not_installed', 'Личный плагин', 'step-5', 'Карточка создана и плагин установлен', plugin),
    ('06-dot-read', 'dot_read_not_verified', 'Реальный вызов Dot', 'step-6', 'Реальный MCP read самого Dot', dot_read),
    ('07-saved-text', 'optional_saved_write', 'Один тест в Избранное', 'step-7', 'Owner-approved отправка с readback', saved_text),
]


def main():
    entries = []
    for stem, blocked, title, anchor, evidence, build in STEPS:
        build().save(stem)
        entries.append({'id': stem, 'blocked_state': blocked, 'title': title,
                        'guide': 'dot-client-guide.md' + ('#' + anchor if anchor else ''),
                        'image': 'assets/dot-guide/' + stem + '.png',
                        'editable_source': 'assets/dot-guide/' + stem + '.svg',
                        'kind': 'teaching_illustration', 'uses_real_account_data': False,
                        'required_evidence': evidence})
    manifest = {'schema_version': 1, 'language': 'ru', 'canvas': {'width': W, 'height': H},
                'label': 'Учебные иллюстрации с условными данными, не скриншоты аккаунта',
                'steps': entries}
    (ROOT / 'docs/dot-guide-steps.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print('Built 8 teaching SVGs and the public step index. No account access used.')


if __name__ == '__main__':
    main()
