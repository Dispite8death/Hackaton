from html import escape


def navigation(active, status):
    modes = (("/", "Сводный аудит"), ("/quick", "Алгоритмический аудит"), ("/admin", "Настройки"))
    links = []
    for path, label in modes:
        current = ' aria-current="page"' if path == active else ""
        links.append(f'<a href="{path}"{current}>{label}</a>')
    return ('<div class="kontur-bar"><div class="kontur-inner">'
            '<a class="kontur-brand" href="/">Контур<small>Аудит сайтов образования</small></a>'
            '<nav class="kontur-links" aria-label="Режимы аудита">' + "".join(links) + '</nav>'
            '<a class="kontur-status" id="access" href="/admin#model">' + escape(status) + '</a></div></div>')
