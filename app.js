const state = { payload: null };
const fmt = new Intl.NumberFormat('bg-BG', { maximumFractionDigits: 0 });

function money(v){ return v == null ? 'Цена: за проверка' : `${fmt.format(v)} лв.`; }
function sqm(v){ return v == null ? '—' : `${fmt.format(v)} кв.м`; }
function escapeHtml(s=''){ return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function scoreLabel(v){ if(v >= 80) return 'висок'; if(v >= 65) return 'добър'; return 'за преглед'; }

function render(){
  const q = document.querySelector('#q').value.trim().toLowerCase();
  const source = document.querySelector('#source').value;
  const category = document.querySelector('#category').value;
  const risk = document.querySelector('#risk').value;
  const maxPrice = Number(document.querySelector('#maxPrice').value || 0);
  const sort = document.querySelector('#sort').value;
  let items = [...(state.payload?.items || [])];

  items = items.filter(x => {
    const hay = `${x.title} ${x.location} ${x.description} ${x.source} ${x.category}`.toLowerCase();
    if(q && !hay.includes(q)) return false;
    if(source && x.source !== source) return false;
    if(category && x.category !== category) return false;
    if(maxPrice && x.price_bgn != null && x.price_bgn > maxPrice) return false;
    if(risk === 'no-ideal' && x.ideal_parts) return false;
    if(risk === 'ideal' && !x.ideal_parts) return false;
    return true;
  });

  if(sort === 'score') items.sort((a,b)=>(b.score ?? 0)-(a.score ?? 0) || (a.price_bgn ?? 1e30)-(b.price_bgn ?? 1e30));
  if(sort === 'price') items.sort((a,b)=>(a.price_bgn ?? 1e30)-(b.price_bgn ?? 1e30));
  if(sort === 'area') items.sort((a,b)=>(b.area_sqm ?? -1)-(a.area_sqm ?? -1));
  if(sort === 'land') items.sort((a,b)=>(b.land_area_sqm ?? -1)-(a.land_area_sqm ?? -1));
  if(sort === 'ppsqm') items.sort((a,b)=>(a.price_per_sqm ?? 1e30)-(b.price_per_sqm ?? 1e30));

  document.querySelector('#visibleCount').textContent = items.length;
  const cards = document.querySelector('#cards');
  if(!items.length){ cards.innerHTML = '<div class="empty">Няма резултати за избраните филтри.</div>'; return; }

  cards.innerHTML = items.map(x => {
    const signals = (x.signals || []).map(s => `<span class="signal ${s.includes('Идеални') ? 'risk' : ''}">${escapeHtml(s)}</span>`).join('');
    const score = x.score ?? 0;
    return `
    <article class="card ${x.ideal_parts ? 'has-risk' : ''}">
      <div class="topline">
        <span class="category">${escapeHtml(x.category || 'Имот')}</span>
        <span class="score">Интерес: ${scoreLabel(score)} · ${score}/100</span>
      </div>
      <span class="source">${escapeHtml(x.source)}</span>
      <h2>${escapeHtml(x.title)}</h2>
      <div class="location">📍 ${escapeHtml(x.location || 'Балчик / общината')}</div>
      <div class="price">${money(x.price_bgn)}</div>
      <div class="facts">
        <div><span>Застр. площ</span><b>${sqm(x.area_sqm)}</b></div>
        <div><span>Двор / парцел</span><b>${sqm(x.land_area_sqm)}</b></div>
        <div><span>Срок</span><b>${escapeHtml(x.deadline || 'за проверка')}</b></div>
      </div>
      ${x.price_per_sqm ? `<div class="ppsqm">≈ ${fmt.format(x.price_per_sqm)} лв./кв.м по наличната площ</div>` : ''}
      ${signals ? `<div class="signals">${signals}</div>` : ''}
      <a class="button" href="${escapeHtml(x.url)}" target="_blank" rel="noopener noreferrer">Отвори оригиналната обява ↗</a>
    </article>`;
  }).join('');
}

async function load(){
  const res = await fetch(`data/listings.json?t=${Date.now()}`, {cache:'no-store'});
  if(!res.ok) throw new Error(`HTTP ${res.status}`);
  state.payload = await res.json();
  const items = state.payload.items || [];
  document.querySelector('#totalCount').textContent = state.payload.count ?? 0;
  document.querySelector('#houseCount').textContent = items.filter(x => String(x.category).startsWith('Къща')).length;
  document.querySelector('#updated').textContent = state.payload.updated_at ? new Date(state.payload.updated_at).toLocaleString('bg-BG') : 'още няма автоматично обновяване';

  const sources = [...new Set(items.map(x=>x.source))].sort();
  document.querySelector('#source').innerHTML = '<option value="">Всички източници</option>' + sources.map(s=>`<option>${escapeHtml(s)}</option>`).join('');
  const categories = [...new Set(items.map(x=>x.category).filter(Boolean))].sort();
  document.querySelector('#category').innerHTML = '<option value="">Всички типове</option>' + categories.map(s=>`<option>${escapeHtml(s)}</option>`).join('');

  const diag = state.payload.diagnostics || {};
  const diagBox = document.querySelector('#diagnostics');
  const summary = diag.summary || {};
  const sourceRows = Object.entries(diag).filter(([k]) => k !== 'summary').map(([name, d]) => {
    if(d.error) return `<div><b>${escapeHtml(name)}</b><span>грешка при източника</span></div>`;
    const candidates = d.index_candidates ?? '—';
    const detail = d.detail_ok ?? '—';
    const fallback = d.fallback_from_index ?? 0;
    const returned = d.returned ?? '—';
    return `<div><b>${escapeHtml(name)}</b><span>кандидати ${candidates} · прочетени ${detail} · резервни ${fallback} · върнати ${returned}</span></div>`;
  }).join('');
  if(Object.keys(diag).length){
    diagBox.hidden = false;
    diagBox.innerHTML = `<div class="diag-summary"><b>Диагностика</b><span>уникални ${summary.unique_before_filters ?? '—'} → показани ${summary.shown_after_filters ?? items.length} · къщи ${summary.houses ?? '—'} · апартаменти ${summary.apartments ?? '—'} · парцели ${summary.land ?? '—'}</span></div>${sourceRows}`;
  } else { diagBox.hidden = true; }

  const errors = state.payload.source_errors || [];
  const warning = document.querySelector('#sourceWarning');
  if(errors.length){
    warning.hidden = false;
    warning.innerHTML = `<strong>Частичен режим:</strong> ${errors.map(escapeHtml).join(' · ')}. Останалите източници са обновени.`;
  } else {
    warning.hidden = true;
  }
  render();
}

document.querySelectorAll('input,select').forEach(el => el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', render));
load().catch(err => {
  document.querySelector('#cards').innerHTML = `<div class="empty">Не успях да заредя данните: ${escapeHtml(String(err))}</div>`;
});
