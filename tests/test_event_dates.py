from datetime import date
from types import SimpleNamespace

import pytest

from audit.checks import Options, SiteAuditor
from audit.event_dates import reconcile_event_dates
from audit.fetcher import Page
from audit.models import SiteResult


@pytest.mark.parametrize('algorithm,model,checked,value,status', [
    (date(2026, 9, 29), date(2025, 9, 29), True, None, 'review'),
    (date(2026, 9, 29), None, True, None, 'review'),
    (None, date(2026, 9, 29), True, None, 'review'),
    (date(2026, 9, 29), date(2026, 9, 29), True, date(2026, 9, 29), 'agreement'),
    (None, None, True, None, 'unknown'),
    (date(2026, 9, 29), None, False, date(2026, 9, 29), 'algorithm'),
])
def test_dates_require_review_on_disagreement(algorithm, model, checked, value, status):
    decision = reconcile_event_dates(algorithm, model, model_checked=checked)
    assert (decision.value, decision.status) == (value, status)
    assert decision.candidates['algorithm'] == (algorithm.isoformat() if algorithm else None)
    if checked:
        assert decision.candidates['model'] == (model.isoformat() if model else None)


def test_wrong_model_year_does_not_become_a_late_publication_violation():
    base = 'https://school.example'
    result = SiteResult(domain='school.example', name='Test', kind='school', sections={'news': base + '/news/'})
    listing = '<main>' + ''.join(
        f'<div><span class="date">{day} октября 2026</span><h3><a href="/news/{day}.html">Встреча {day}</a></h3></div>'
        for day in (1, 2, 3)) + '</main>'
    article = '<main><h1>Встреча</h1><p>29 сентября состоялась встреча с инспектором.</p></main>'
    fetcher = SimpleNamespace(get=lambda url: Page(url, 200, article if '.html' in url else listing))
    checker = SimpleNamespace(check_news=lambda *args: {
        'event_date': date(2025, 9, 29), 'event_date_basis': 'wrong year', 'claims': []})
    auditor = SiteAuditor(fetcher, result, Options(today=date(2026, 10, 4), llm_news_per_site=3), checker)
    auditor.check_news(Page(base, 200, '<main/>'))
    assert len(result.news) == 3
    assert result.metrics['news_event_date_review'] == 3
    assert result.metrics['news_late'] == 0
    assert not any(f.check in ('news_late', 'news_no_event_date') for f in result.findings)
    for item in result.news:
        assert item.event_date is None and item.delay_days is None
        assert item.event_date_candidates == {'algorithm': '2026-09-29', 'model': '2025-09-29'}


def test_review_candidates_survive_report_round_trip(tmp_path):
    import json
    from datetime import datetime
    from audit.models import NewsItem
    from audit.report import load_results, render_all

    site = SiteResult(domain='school.example', name='Test', kind='school')
    site.metrics['news_event_date_review'] = 1
    site.news = [NewsItem(url='https://school.example/news/1.html', title='Встреча',
                         published=datetime(2026, 10, 1), text='29 сентября состоялась встреча.',
                         event_date_status='review', event_date_source='Требуется проверка',
                         event_date_candidates={'algorithm': '2026-09-29', 'model': '2025-09-29'})]
    path = tmp_path / 'results.json'
    path.write_text(json.dumps({'summary': {'audit_date': '2026-10-04', 'duration_sec': 0, 'http': {'requests': 0, 'cache_hits': 0}, 'llm': None}, 'sites': [site.to_dict()]}, ensure_ascii=False))
    sites, summary = load_results(path)
    assert sites[0].news[0].event_date_candidates == site.news[0].event_date_candidates
    render_all(sites, summary, tmp_path / 'report')
    html = (tmp_path / 'report/sites/school.html').read_text()
    assert 'требуется проверка' in html and '29.09.2026' in html and '29.09.2025' in html
