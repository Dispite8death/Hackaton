"""Compare rendered text only with peers that perform the same role in the same block."""

_COMPARE = r"""items => {
    const groups = new Map();
    const selectorFor = el => {
        const path = [];
        for (let n=el; n && n.nodeType===1; n=n.parentElement) {
            if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length===1) {
                path.unshift('#' + CSS.escape(n.id)); break;
            }
            const index=Array.from(n.parentElement?.children || [n]).filter(x=>x.localName===n.localName).indexOf(n)+1;
            path.unshift(n.localName + ':nth-of-type(' + index + ')');
        }
        return path.join(' > ');
    };
    function add(group, role, el) {
        if (!group || !el) return;
        if (!el.checkVisibility({checkOpacity:true, checkVisibilityCSS:true})) return;
        const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT), signatures=new Set();
        let t;
        while ((t=walker.nextNode())) {
            if (!t.textContent.trim() || !t.parentElement.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) continue;
            const s=getComputedStyle(t.parentElement);
            signatures.add(JSON.stringify([s.fontFamily,s.fontSize,s.color]));
        }
        if (!signatures.size) return;
        const selector=selectorFor(group), key=role+'|'+selector;
        if (!groups.has(key)) groups.set(key,{role,container:selector,elements:[]});
        groups.get(key).elements.push({selector:selectorFor(el),text:el.textContent.trim().slice(0,180),
                                      styles:Array.from(signatures).sort().map(x=>JSON.parse(x))});
    }
    for (const item of items) {
        const cards=document.querySelectorAll(item.card), nodes=document.querySelectorAll(item.role);
        if (cards.length!==1 || nodes.length!==1 || !cards[0].contains(nodes[0])) continue;
        const group=cards[0].closest('.tpl-component-gw-staff,.gw-person-list,.gw-people-list') || cards[0].parentElement;
        add(group,'staff_role',nodes[0]);
    }
    const main=document.querySelector('main,[role="main"]') || document.body;
    const widgets='.tpl-component-gw-events,[data-events],.events,.events-list,.events-section,[data-section="events"]';
    for (const block of main.querySelectorAll(widgets)) {
        if (block.matches('.tpl-block-full') || block.closest('footer,nav,aside,.news,.tpl-component-gw-news')) continue;
        let cards=block.querySelectorAll('a.object-index,.object,.event-card,.event-item,[itemtype$="/Event"]');
        if (!cards.length) cards=block.querySelectorAll('article');
        if (!cards.length) cards=block.querySelectorAll('li');
        const seen=new Set();
        for (const card of cards) {
            if (card.closest(widgets)!==block) continue;
            const title=card.querySelector('.name,.event-title,.object-title,.object-name,[itemprop="name"]') ||
                        card.querySelector('h2,h3,h4');
            if (!title || seen.has(title) || title.closest('.description,nav,footer,.news')) continue;
            seen.add(title);
            add(block,'event_title',title);
        }
    }
    return Array.from(groups.values()).filter(g=>g.elements.length>=2).map(g=>({
        ...g, result:new Set(g.elements.map(e=>JSON.stringify(e.styles))).size>1 ? 'different' : 'uniform'
    }));
}"""


def collect_peer_styles(page, cards):
    fields = []
    for card in cards or ():
        selectors = {entry.field: entry.selector for entry in card.elements}
        if selectors.get("role") and selectors.get("card"):
            fields.append({"role": selectors["role"], "card": selectors["card"]})
    return page.evaluate(_COMPARE, fields)

