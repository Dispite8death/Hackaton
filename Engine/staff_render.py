"""Read-only geometry evidence for staff elements in the current browser viewport."""


_MEASURE = r"""({url, cards}) => {
    const norm = s => (s || '').normalize('NFKC').replace(/\s+/g, ' ').trim().toLowerCase();
    const digits = s => (s || '').replace(/\D/g, '');
    const absolute = (s, base) => { try { return new URL(s, base).href; } catch { return ''; } };
    const documentUrl = u => absolute(u, document.baseURI).split('#', 1)[0];
    const samePage = documentUrl(url) === documentUrl(location.href);
    const viewport = {left:0, top:0, right:document.documentElement.clientWidth, bottom:document.documentElement.clientHeight};
    const overlaps = (r,x,y) => x >= r.left && x < r.right && y >= r.top && y < r.bottom;
    const styles = new Map(), css = n => {
        if (!styles.has(n)) styles.set(n, getComputedStyle(n));
        return styles.get(n);
    };
    const bounds = n => n.getBoundingClientRect();
    function effect(n) {
        let opacity = 1, complex = false, hidden = false;
        for (let a=n; a; a=a.parentElement) {
            const s=css(a);
            opacity *= Number(s.opacity);
            if (s.display === 'none' || s.contentVisibility === 'hidden') hidden=true;
            if (s.filter !== 'none' || s.clipPath !== 'none' || s.maskImage !== 'none' ||
                s.mixBlendMode !== 'normal' || (s.backdropFilter && s.backdropFilter !== 'none'))
                complex=true;
            if (s.transform !== 'none') {
                const matrix = new DOMMatrix(s.transform);
                if (!matrix.is2D || matrix.a <= 0 || matrix.d <= 0 || matrix.b || matrix.c) complex=true;
            }
        }
        return {opacity, complex, hidden:hidden || opacity === 0 || css(n).visibility !== 'visible'};
    }
    const rectList = r => [r.left,r.top,r.right-r.left,r.bottom-r.top].map(x=>Math.round(x*100)/100);
    const intersect = (a,b) => ({left:Math.max(a.left,b.left), top:Math.max(a.top,b.top),
        right:Math.min(a.right,b.right), bottom:Math.min(a.bottom,b.bottom)});
    const empty = r => r.right <= r.left || r.bottom <= r.top;
    const query = s => { try { return Array.from(document.querySelectorAll(s)); } catch { return []; } };
    function matches(n, field, expected) {
        if (!expected) return false;
        if (field === 'photo') {
            const source = n.currentSrc || n.getAttribute('src') || n.getAttribute('data-src');
            return n.localName === 'img' && source && absolute(source,document.baseURI) === absolute(expected,document.baseURI);
        }
        if (field === 'phone') {
            const d=digits(expected);
            return d.length >= 5 && [n.textContent,n.getAttribute('href')].some(v=>digits(v)===d);
        }
        const current=norm(n.textContent), wanted=norm(expected);
        if (field==='card') return current.includes(wanted); // identity is checked on own name and role below.
        if (field==='email') return current===wanted || norm(n.getAttribute('href')).split('?')[0]==='mailto:'+wanted;
        const clean=s=>norm(s).replace(/[.,;:]+$/,'').trim();
        if (clean(current)===clean(wanted)) return true;
        // A label and its value sometimes share one wrapper; compare the actual value.
        if (field==='education' && clean(current.slice(current.indexOf(':')+1))===clean(wanted)) return true;
        return Array.from(n.childNodes).some(c=>c.nodeType===3 && clean(c.textContent)===clean(wanted));
    }
    const transparent = c => c === 'transparent' || /^rgba\([^)]*,\s*0(?:\.0+)?\)$/.test(c);
    const solid = c => /^rgb\(/.test(c) || /^rgba\([^)]*,\s*1(?:\.0+)?\)$/.test(c);
    function mightPaint(n,s) {
        return !transparent(s.backgroundColor) || s.backgroundImage !== 'none' ||
            s.boxShadow !== 'none' || parseFloat(s.borderTopWidth) > 0 || parseFloat(s.borderLeftWidth) > 0 ||
            ['IMG','VIDEO','SVG','CANVAS','IFRAME'].includes(n.tagName.toUpperCase()) ||
            Array.from(n.childNodes).some(c=>c.nodeType===3 && c.textContent.trim());
    }
    // elementsFromPoint skips pointer-events:none, including painted overlays.
    // Without paint order for these layers an overlapping candidate remains uncertain.
    const unhitPaint = [];
    for (const n of document.querySelectorAll('body,body *')) {
        const s=css(n);
        if (s.pointerEvents==='none') {
            const r=bounds(n);
            if (!empty(intersect(r,viewport)) && !effect(n).hidden && mightPaint(n,s)) unhitPaint.push([n,r,false]);
        }
        // A fixed generated box can paint outside its host without appearing in hit tests.
        for (const which of ['::before','::after']) {
            const p=getComputedStyle(n,which);
            if (p.position==='fixed' && p.display!=='none' && p.visibility==='visible' && Number(p.opacity)>0 &&
                p.content!=='none' && p.content!=='normal' && !effect(n).hidden) {
                unhitPaint.push([n,viewport,true]);
            }
        }
    }
    function atPoint(target,x,y) {
        const hits=document.elementsFromPoint(x,y);
        const owned=hits.findIndex(n=>n===target || target.contains(n));
        if (owned < 0) return 'uncertain';
        const above=hits.slice(0,owned);
        let ambiguous=false;
        for (const other of above) {
            if (other.contains(target)) continue;
            const e=effect(other), s=css(other);
            if (e.hidden) continue; // opacity:0 may intercept a click without obscuring pixels.
            if (e.opacity === 1 && !e.complex && solid(s.backgroundColor)) return 'covered';
            // Transparent backgrounds may contain text, pseudo-elements, images or borders.
            ambiguous=true;
        }
        if (unhitPaint.some(([n,r,pseudo])=>(pseudo || (!n.contains(target) && !target.contains(n))) && overlaps(r,x,y)))
            ambiguous=true;
        return ambiguous ? 'uncertain' : 'clear';
    }
    const output=[];
    for (const card of cards) {
        const roots=query(card.selector);
        const root=roots.length===1 ? roots[0] : null;
        const names=card.elements.filter(e=>e.field==='name');
        const identity=!!root && names.length>0 && names.every(e=>{
            const found=query(e.selector);
            return found.length===1 && root.contains(found[0]) && matches(found[0],e.field,e.expected_value);
        }) && card.elements.filter(e=>e.field==='role').every(e=>{
            const found=query(e.selector);
            return found.length===1 && root.contains(found[0]) && matches(found[0],e.field,e.expected_value);
        });
        for (const entry of card.elements) {
            const d={reason:'',expected_match:false,hit_test_uncertain:false,samples:0,clear:0,covered:0,uncertain:0,
                viewport:[viewport.right,viewport.bottom],scroll:[scrollX,scrollY]};
            const finish=(status,reason) => {
                d.reason=reason; d.hit_test_uncertain = status==='unknown';
                output.push({...entry,card_selector:card.selector,status,details:d});
            };
            if (!samePage) { finish('unknown','different_document'); continue; }
            if (!roots.length) { finish('not_found','card_selector_missing'); continue; }
            if (roots.length!==1 || !identity) { finish('unknown','card_identity_unconfirmed'); continue; }
            const found=query(entry.selector);
            d.matches=found.length;
            if (!found.length) { finish('not_found','element_selector_missing'); continue; }
            const el=found[0];
            // Education may be an adjacent block of one person's dedicated profile.
            const profileField=entry.field==='education' && card.profile && documentUrl(card.profile)===documentUrl(url) &&
                root.closest('main') && root.closest('main')===el.closest('main');
            if (found.length!==1 || (!root.contains(el) && !profileField) ||
                !matches(el,entry.field,entry.expected_value)) { finish('unknown','element_identity_unconfirmed'); continue; }
            d.expected_match=true;
            const s=css(el), e=effect(el), r=bounds(el);
            d.display=s.display; d.visibility=s.visibility; d.effective_opacity=e.opacity;
            d.rect=rectList(r); d.pointer_events=s.pointerEvents;
            if (e.hidden) { finish('hidden','css_hidden'); continue; }
            if (e.opacity<1 || (entry.field!=='photo' && entry.field!=='card' &&
                (transparent(s.color) || parseFloat(s.fontSize)===0))) {
                finish('unknown','translucent_or_unpainted_content'); continue;
            }
            if (!r.width || !r.height || e.complex || el.getClientRects().length>1) {
                finish('unknown','complex_or_empty_geometry'); continue;
            }
            if (entry.field==='photo' && (!el.complete || el.naturalWidth===0)) {
                finish('unknown','image_pixels_unavailable'); continue;
            }
            let rect=intersect(r,viewport);
            if (empty(rect)) { finish('out_of_view','outside_viewport'); continue; }
            for (let a=el.parentElement;a && a!==document.body && a!==document.documentElement;a=a.parentElement) {
                const as=css(a), ar=bounds(a);
                if (as.overflowX!=='visible') {
                    rect.left=Math.max(rect.left,ar.left+a.clientLeft);
                    rect.right=Math.min(rect.right,ar.left+a.clientLeft+a.clientWidth);
                }
                if (as.overflowY!=='visible') {
                    rect.top=Math.max(rect.top,ar.top+a.clientTop);
                    rect.bottom=Math.min(rect.bottom,ar.top+a.clientTop+a.clientHeight);
                }
            }
            if (empty(rect)) { finish('hidden','clipped_by_ancestor'); continue; }
            d.viewport_rect=rectList(rect);
            d.in_view_fraction=(rect.right-rect.left)*(rect.bottom-rect.top)/(r.width*r.height);
            for (const fx of [.16,.5,.84]) for (const fy of [.16,.5,.84]) {
                const outcome=atPoint(el,rect.left+fx*(rect.right-rect.left),rect.top+fy*(rect.bottom-rect.top));
                d[outcome]++; d.samples++;
            }
            if (d.uncertain) finish('unknown','ambiguous_paint_order_or_hit_test');
            else if (d.covered===d.samples) finish('occluded','opaque_layer_at_sampled_points');
            else if (d.covered || d.in_view_fraction<.995) finish('partial','partly_in_view_or_covered');
            else finish('visible','geometry_clear_at_sampled_points');
        }
    }
    return output;
}"""


def measure_staff_elements(page, cards, page_url):
    """Observe the current page, without scrolling, clicking or changing its styles."""
    batch = []
    for card in cards:
        elements = [dict(field=e.field, selector=e.selector, expected_value=e.text) for e in card.elements]
        selector = next((e["selector"] for e in elements if e["field"] == "card"), None)
        if selector:
            batch.append(dict(selector=selector, profile=card.profile_url, elements=elements))
    if not batch:
        return []
    try:
        return page.evaluate(_MEASURE, {"url": page_url, "cards": batch})
    except Exception as error:
        details = {"reason": "browser_evaluation_failed", "error_type": type(error).__name__,
                   "expected_match": False, "hit_test_uncertain": True,
                   "samples": 0, "clear": 0, "covered": 0, "uncertain": 0}
        return [{**entry, "card_selector": card["selector"], "status": "unknown", "details": details.copy()}
                for card in batch for entry in card["elements"]]
