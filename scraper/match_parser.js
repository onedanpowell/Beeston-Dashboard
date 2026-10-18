(doc) => {
  // Reads an England Hockey match page (/fixtures/<id>): score, both line-ups and each player's events.
  // Uses textContent only, so it works on a live page and on HTML parsed with DOMParser.
  const txt = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
  const EVENT = { 'field-goal': 'FG', 'penalty-corner': 'PC', 'penalty-stroke': 'PS', 'green-card': 'GC', 'yellow-card': 'YC', 'red-card': 'RC' };
  const scoreText = txt(doc.querySelector('.c-match-header__results-score'));
  const sm = scoreText.match(/(\d+)\s*[–-]\s*(\d+)/);
  const blocks = [...doc.querySelectorAll('.c-match-details__more-info')].filter((b) => b.querySelector('.c-team-info__lineup-items'));
  const teams = blocks.map((b, i) => {
    const logo = b.querySelector('.c-team-info__logo-container');
    const img = logo && logo.querySelector('img[alt^="Logo of"]');
    const name = img ? img.getAttribute('alt').replace(/^Logo of\s+/, '').trim() : txt(logo).split('  ')[0];
    const players = [...b.querySelectorAll('.c-team-info__lineup-item')].map((it) => {
      const info = it.querySelector('.c-lineup-item__player-info');
      const numEl = info && info.querySelector('span');
      const no = txt(numEl);
      const pname = info ? [...info.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(' ').replace(/\s+/g, ' ').trim() : '';
      const roles = [...it.querySelectorAll('.c-lineup-item__position')].map(txt).join(' ').toUpperCase();
      const events = [...it.querySelectorAll('.c-lineup-item__stats use')].map((u) => {
        const ref = ((u.getAttribute('xlink:href') || u.getAttribute('href') || '').split('#')[1] || '').replace(/-match-details$/, '');
        const row = u.closest('div');
        const t = txt(row && row.querySelector('.c-lineup-item__time')).replace(/[’']/g, '');
        return { type: EVENT[ref] || ref, minute: /^\d+$/.test(t) ? Number(t) : null };
      });
      const withheld = /^name withheld$/i.test(pname);
      return { no: /^\d+$/.test(no) ? Number(no) : null, name: withheld ? null : pname, withheld,
               gk: /\bGK\b/.test(roles), capt: /\bCAPT\b/.test(roles), events };
    });
    return { name, side: i === 0 ? 'home' : 'away', players };
  });
  return { homeScore: sm ? Number(sm[1]) : null, awayScore: sm ? Number(sm[2]) : null, teams };
}
