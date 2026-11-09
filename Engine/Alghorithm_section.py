from contextlib import closing

if __package__:
    from .config import connect_db
    from .crawl_store import initialize_db
    from .saved_pages import get_organization_id
    from .scan_staff import head_name_key, scan_organization
    from .staff_extract import extract_staff
else:
    from config import connect_db
    from crawl_store import initialize_db
    from saved_pages import get_organization_id
    from scan_staff import head_name_key, scan_organization
    from staff_extract import extract_staff


def scanHeadByName(name: str) -> bool:
    """Сохранить карточки последнего аудита. True — найден один кандидат в руководители."""
    with closing(connect_db()) as conn:
        initialize_db(conn)
        organization_id = get_organization_id(conn, name)
        if organization_id is None:
            raise ValueError(f"Учреждение не найдено: {name}")
        result = scan_organization(conn, organization_id)
    return result.head_source in {"home", "inner"}


def scanSchool(html: str) -> bool:
    """Проверить переданный HTML без обращения к сети или к базе."""
    names = {head_name_key(card.name) for card in extract_staff(html, "", "home")
             if card.is_head and card.name}
    return len(names) == 1
