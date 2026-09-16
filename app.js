const state = { payload: null };
const fmt = new Intl.NumberFormat('bg-BG', { maximumFractionDigits: 0 });

function money(v){ return v == null ? 'Цена не е извлечена' : `${fmt.format(v)} лв.`; }
function area(v){ return v == null ? 'Площ не е извлечена' : `${fmt.format(v)} кв.м`; }
function escapeHtml(s=''){ return s.replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }

function render(){
  const q = document.querySelector('#q').value.trim().toLowerCase();
  const source = document.querySelector('#source').value;
  const maxPrice = Number(document.querySelector('#maxPrice').value || 0);
  const sort = document.querySelector('#sort').value;
  let items = [...(state.payload?.items || [])];
  items = items.filter(x => {
    const hay = `${x.title} ${x.location} ${x.description} ${x.source}`.toLowerCase();
    if(q && !hay.includes(q)) return false;
    if(source && x.source !== source) return false;
    if(maxPrice && x.price_bgn != null && x.price_bgn > maxPrice) return false;
    return true;
  });
  if(sort === 'price') items.sort((a,b)=>(a.price_bgn ?? 1e30)-(b.price_bgn ?? 1e30));
  if(sort === 'area') items.sort((a,b)=>(b.area_sqm ?? -1)-(a.area_sqm ?? -1));
  if(sort === 'ppsqm') items.sort((a,b)=>(a.price_per_sqm ?? 1e30)-(b.price_per_sqm ?? 1e30));

  document.querySelector('#visibleCount').textContent = items.length;
  const cards = document.querySelector('#cards');
  if(!items.length){ cards.innerHTML = '<div class="empty">Няма резултати за избраните филтри.</div>'; return; }
  cards.innerHTML = items.map(x => `
    <article class="card">
      <span class="badge">${escapeHtml(x.source)}</span>
      <h2>${escapeHtml(x.title)}</h2>
      <div class="meta">📍 ${escapeHtml(x.location || 'Балчик / общината')}</div>
      <div class="price">${money(x.price_bgn)}</div>
      <div class="meta">${area(x.area_sqm)}${x.deadline ? ` · Срок: ${escapeHtml(x.deadline)}` : ''}</div>
      ${x.price_per_sqm ? `<div class="ppsqm">≈ ${fmt.format(x.price_per_sqm)} лв./кв.м</div>` : ''}
      <a class="button" href="${x.url}" target="_blank" rel="noopener noreferrer">Отвори оригиналната обява</a>
    </article>`).join('');
}

async function load(){
  const res = await fetch(`data/listings.json?t=${Date.now()}`, {cache:'no-store'});
  if(!res.ok) throw new Error(`HTTP ${res.status}`);
  state.payload = await res.json();
  document.querySelector('#totalCount').textContent = state.payload.count ?? 0;
  document.querySelector('#updated').textContent = state.payload.updated_at ? new Date(state.payload.updated_at).toLocaleString('bg-BG') : 'още няма автоматично обновяване';
  const sources = [...new Set((state.payload.items || []).map(x=>x.source))].sort();
  document.querySelector('#source').innerHTML = '<option value="">Всички източници</option>' + sources.map(s=>`<option>${escapeHtml(s)}</option>`).join('');
  render();
}

document.querySelectorAll('input,select').forEach(el => el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', render));
load().catch(err => {
  document.querySelector('#cards').innerHTML = `<div class="empty">Не успях да заредя данните: ${escapeHtml(String(err))}</div>`;
});
