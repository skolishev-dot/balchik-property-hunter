const state = { payload: null, region: 'Балчик' };
function regionItems(){ return (state.payload?.items || []).filter(x => (x.region || 'Балчик') === state.region); }
function regionFallback(){ return state.region === 'Варна' ? 'Варна / района' : 'Балчик / общината'; }
const REVIEW_STORAGE_KEY = 'balchik-property-hunter-reviewed-v1';

function loadReviewed(){
  try { return JSON.parse(localStorage.getItem(REVIEW_STORAGE_KEY) || '{}') || {}; }
  catch { return {}; }
}
function saveReviewed(map){ localStorage.setItem(REVIEW_STORAGE_KEY, JSON.stringify(map)); }
function listingKey(x){
  return String(x.url || `${x.source || ''}|${x.title || ''}|${x.location || ''}`).trim();
}
function isReviewed(x){ return !!loadReviewed()[listingKey(x)]; }
function setReviewed(x, checked){
  const map = loadReviewed();
  const key = listingKey(x);
  if(checked) map[key] = { checked: true, at: new Date().toISOString() };
  else delete map[key];
  saveReviewed(map);
}
function reviewControl(x){
  const key = listingKey(x);
  const checked = isReviewed(x);
  return `<label class="review-control"><input class="review-toggle" type="checkbox" data-review-key="${escapeHtml(key)}" ${checked ? 'checked' : ''}><span>✓ Прочетено / проверено</span></label>`;
}
const fmt = new Intl.NumberFormat('bg-BG', { maximumFractionDigits: 0 });

function money(v){ return v == null ? 'Цена: за проверка' : `${fmt.format(v)} лв.`; }
function sqm(v){ return v == null ? '—' : `${fmt.format(v)} кв.м`; }
function escapeHtml(s=''){ return String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function scoreLabel(v){ if(v >= 70) return 'висок приоритет'; if(v >= 35) return 'заслужава преглед'; return 'нисък приоритет'; }


const BALCHIK_BUYER_TARGET_CATEGORIES = new Set([
  'Къща + двор/парцел','Къща/вила','Сграда + парцел','УПИ/дворно място','Парцел/земя'
]);
const VARNA_BUYER_TARGET_CATEGORIES = new Set([
  'Апартамент','Къща + двор/парцел','Къща/вила'
]);
function buyerTargetCategories(){ return state.region === 'Варна' ? VARNA_BUYER_TARGET_CATEGORIES : BALCHIK_BUYER_TARGET_CATEGORIES; }

function buyerChecks(x){
  const checks = [];
  if(x.price_bgn == null) checks.push('Провери началната цена');
  if(!x.deadline) checks.push('Провери срока за участие');
  if(x.ideal_parts) checks.push('Провери идеалните части и точния дял');
  if(x.category === 'Парцел/земя') checks.push('Провери регулация, достъп и възможност за строеж');
  if(x.category === 'УПИ/дворно място') checks.push('Провери параметрите за застрояване и комуникациите');
  if(x.category === 'Сграда + парцел') checks.push('Провери предназначението и състоянието на сградата');
  if(String(x.category || '').startsWith('Къща')) checks.push('Провери владение, тежести и състояние на сградата');
  if(!x.land_area_sqm && buyerTargetCategories().has(x.category)) checks.push('Провери точната площ на двора/парцела');
  return checks.slice(0,3);
}

function featuredCard(x, i, variant='buyer'){
  const reasons = (x.deal_reasons || []).slice(0,4).map(r => `<span>${escapeHtml(r)}</span>`).join('');
  const checks = buyerChecks(x).map(r => `<span>${escapeHtml(r)}</span>`).join('');
  const price = x.price_bgn == null ? 'Цена за проверка' : `${fmt.format(x.price_bgn)} лв.`;
  return `
    <article class="featured-card ${variant === 'other' ? 'featured-other' : 'featured-buyer'} ${isReviewed(x) ? 'is-reviewed' : ''}">
      <div class="featured-rank">#${i+1}</div>
      <div class="featured-main">
        <div class="featured-topline">
          <span class="featured-category">${escapeHtml(x.category || 'Имот')}</span>
          <strong>${x.score ?? 0}/100</strong>
        </div>
        <h3>${escapeHtml(x.title)}</h3>
        <div class="featured-location">📍 ${escapeHtml(x.location || regionFallback())}</div>
        <div class="featured-stats">
          <div><span>Цена</span><b>${price}</b></div>
          <div><span>Срок</span><b>${escapeHtml(x.deadline || 'за проверка')}</b></div>
          <div><span>Двор / парцел</span><b>${sqm(x.land_area_sqm)}</b></div>
        </div>
        ${reasons ? `<div class="featured-explain"><b>Защо е интересен</b><div class="featured-reasons">${reasons}</div></div>` : ''}
        ${checks ? `<div class="featured-explain checks"><b>Какво да проверя</b><div class="featured-checks">${checks}</div></div>` : ''}
        ${reviewControl(x)}
        <a href="${escapeHtml(x.url)}" target="_blank" rel="noopener noreferrer">Отвори обявата ↗</a>
      </div>
    </article>`;
}

function renderFeatured(){
  const buyerHost = document.querySelector('#featuredCards');
  const otherHost = document.querySelector('#otherActiveCards');
  if(!buyerHost) return;
  const active = [...regionItems()]
    .filter(x => !x.expired)
    .sort((a,b)=>(b.score ?? 0)-(a.score ?? 0) || (a.price_bgn ?? 1e30)-(b.price_bgn ?? 1e30));

  const buyer = active
    .filter(x => x.deal_candidate && buyerTargetCategories().has(x.category))
    .slice(0,6);
  const other = active
    .filter(x => !buyerTargetCategories().has(x.category))
    .slice(0,6);

  buyerHost.innerHTML = buyer.length
    ? buyer.map((x,i)=>featuredCard(x,i,'buyer')).join('')
    : '<div class="featured-empty">В момента няма активна целева обява над приоритетния праг. Следим къщи, имоти с двор/УПИ и подходящи парцели.</div>';

  if(otherHost){
    otherHost.innerHTML = other.length
      ? other.map((x,i)=>featuredCard(x,i,'other')).join('')
      : '<div class="featured-empty">Няма други активни имоти извън основния фокус.</div>';
  }
}

function render(){
  renderFeatured();
  const q = document.querySelector('#q').value.trim().toLowerCase();
  const source = document.querySelector('#source').value;
  const category = document.querySelector('#category').value;
  const focus = document.querySelector('#focus').value;
  const risk = document.querySelector('#risk').value;
  const reviewStatus = document.querySelector('#reviewStatus')?.value || '';
  const maxPrice = Number(document.querySelector('#maxPrice').value || 0);
  const sort = document.querySelector('#sort').value;
  let items = [...regionItems()];

  items = items.filter(x => {
    const hay = `${x.title} ${x.location} ${x.description} ${x.source} ${x.category}`.toLowerCase();
    if(q && !hay.includes(q)) return false;
    if(source && x.source !== source) return false;
    if(category && x.category !== category) return false;
    if(focus === 'deals' && !x.deal_candidate) return false;
    if(focus === 'active' && x.expired) return false;
    if(focus === 'homes' && !['Къща + двор/парцел','Къща/вила','Апартамент'].includes(x.category)) return false;
    if(focus === 'houseyard' && x.category !== 'Къща + двор/парцел') return false;
    if(focus === 'buildable' && !['Къща + двор/парцел','Къща/вила','Сграда + парцел','УПИ/дворно място'].includes(x.category)) return false;
    if(focus === 'noagri' && x.category === 'Земеделска земя') return false;
    if(maxPrice && x.price_bgn != null && x.price_bgn > maxPrice) return false;
    if(risk === 'no-ideal' && x.ideal_parts) return false;
    if(risk === 'ideal' && !x.ideal_parts) return false;
    if(reviewStatus === 'unread' && isReviewed(x)) return false;
    if(reviewStatus === 'reviewed' && !isReviewed(x)) return false;
    return true;
  });

  if(sort === 'score') items.sort((a,b)=>(Number(b.deal_candidate)-Number(a.deal_candidate)) || (Number(a.expired)-Number(b.expired)) || (b.score ?? 0)-(a.score ?? 0) || (a.price_bgn ?? 1e30)-(b.price_bgn ?? 1e30));
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
    const dealReasons = (x.deal_reasons || []).map(r => `<span class="deal-reason">${escapeHtml(r)}</span>`).join('');
    const statusBadge = x.expired ? '<span class="status expired">Изтекъл срок</span>' : (x.deal_candidate ? '<span class="status hot">🔥 Приоритет за преглед</span>' : '');
    return `
    <article class="card ${x.ideal_parts ? 'has-risk' : ''} ${isReviewed(x) ? 'is-reviewed' : ''}">
      <div class="topline">
        <span class="category">${escapeHtml(x.category || 'Имот')}</span>
        <span class="score">Deal Score: ${score}/100 · ${scoreLabel(score)}</span>
      </div>
      ${statusBadge}
      <span class="source">${escapeHtml(x.source)}</span>
      <h2>${escapeHtml(x.title)}</h2>
      <div class="location">📍 ${escapeHtml(x.location || regionFallback())}</div>
      <div class="price-label">Начална / извлечена цена</div><div class="price">${money(x.price_bgn)}</div>
      <div class="facts">
        <div><span>Застр. площ</span><b>${sqm(x.area_sqm)}</b></div>
        <div><span>Двор / парцел</span><b>${sqm(x.land_area_sqm)}</b></div>
        <div><span>Срок</span><b>${escapeHtml(x.deadline || 'за проверка')}</b></div>
      </div>
      ${x.price_per_sqm ? `<div class="ppsqm">≈ ${fmt.format(x.price_per_sqm)} лв./кв.м по наличната площ</div>` : ''}
      ${(x.extraction_source || x.document_count) ? `<div class="document-meta">📄 ${escapeHtml(x.extraction_source || 'документ')} ${x.document_count ? `· ${x.document_count} PDF` : ''}</div>` : ''}
      ${dealReasons ? `<div class="deal-reasons">${dealReasons}</div>` : ''}
      ${signals ? `<div class="signals">${signals}</div>` : ''}
      ${reviewControl(x)}
      <a class="button" href="${escapeHtml(x.url)}" target="_blank" rel="noopener noreferrer">Отвори оригиналната обява ↗</a>
    </article>`;
  }).join('');
}

function refreshRegionView(){
  const focusText = document.querySelector('#buyerFocusText');
  const otherText = document.querySelector('#otherActiveText');
  const searchInput = document.querySelector('#q');
  if(state.region === 'Варна'){
    if(focusText) focusText.textContent = 'Активни апартаменти в гр. Варна с най-силен Deal Score; къщи във Варна също остават видими.';
    if(otherText) otherText.textContent = 'Други активни имоти във Варна извън основния апартаментен фокус.';
    if(searchInput) searchInput.placeholder = 'напр. апартамент, Левски, 80 кв.м';
  } else {
    if(focusText) focusText.textContent = 'Само целевите за търсенето ти активни имоти: къщи, сграда + парцел, УПИ/двор и подходящи парцели.';
    if(otherText) otherText.textContent = 'Активни обяви извън основния фокус — например апартаменти. Остават видими, но не изместват къщите и парцелите.';
    if(searchInput) searchInput.placeholder = 'напр. къща, Дропла, двор';
  }
  const items = regionItems();
  document.querySelector('#totalCount').textContent = items.length;
  document.querySelector('#visibleCount').textContent = items.length;
  document.querySelector('#houseCount').textContent = items.filter(x => String(x.category).startsWith('Къща')).length;
  document.querySelector('#pricedCount').textContent = items.filter(x => x.price_bgn != null).length;
  document.querySelector('#dealCount').textContent = items.filter(x => x.deal_candidate && !x.expired && buyerTargetCategories().has(x.category)).length;
  const reviewedCount = document.querySelector('#reviewedCount');
  if(reviewedCount) reviewedCount.textContent = items.filter(isReviewed).length;
  document.querySelector('#updated').textContent = state.payload?.updated_at ? new Date(state.payload.updated_at).toLocaleString('bg-BG') : 'още няма автоматично обновяване';

  document.querySelectorAll('.region-tab').forEach(btn => btn.classList.toggle('active', btn.dataset.region === state.region));
  const heading = document.querySelector('#regionHeading');
  if(heading) heading.textContent = state.region;
  const intro = document.querySelector('#regionIntro');
  if(intro) intro.textContent = state.region === 'Варна'
    ? 'Публични продажби във Варна и близките населени места. Резултатите и филтрите са отделени от Балчик.'
    : 'Публични продажби около Балчик. Резултатите и филтрите са отделени от Варна.';

  const sourceSel = document.querySelector('#source');
  const oldSource = sourceSel.value;
  const sources = [...new Set(items.map(x=>x.source))].sort();
  sourceSel.innerHTML = '<option value="">Всички източници</option>' + sources.map(v=>`<option>${escapeHtml(v)}</option>`).join('');
  if(sources.includes(oldSource)) sourceSel.value = oldSource;
  const categorySel = document.querySelector('#category');
  const oldCategory = categorySel.value;
  const categories = [...new Set(items.map(x=>x.category).filter(Boolean))].sort();
  categorySel.innerHTML = '<option value="">Всички типове</option>' + categories.map(v=>`<option>${escapeHtml(v)}</option>`).join('');
  if(categories.includes(oldCategory)) categorySel.value = oldCategory;

  const diag = state.payload?.diagnostics || {};
  const diagBox = document.querySelector('#diagnostics');
  const summary = diag.summary?.by_region?.[state.region] || {};
  const sourceRows = Object.entries(diag).filter(([k,d]) => k !== 'summary' && ((d && d.region === state.region) || k.includes(state.region))).map(([name, d]) => {
    if(d.error) return `<div><b>${escapeHtml(name)}</b><span>грешка при източника</span></div>`;
    const candidates = d.index_candidates ?? '—';
    const detail = d.detail_ok ?? '—';
    const fallback = d.fallback_from_index ?? 0;
    const returned = d.returned ?? '—';
    const pdf = d.pdf_documents ?? 0;
    const pdfText = d.pdf_text_items ?? 0;
    const prices = d.prices_extracted ?? 0;
    return `<div><b>${escapeHtml(name)}</b><span>кандидати ${candidates} · детайл ${detail} · резервни ${fallback} · върнати ${returned} · PDF ${pdf}/${pdfText} текстови · цени ${prices}</span></div>`;
  }).join('');
  if(Object.keys(summary).length || sourceRows){
    diagBox.hidden = false;
    diagBox.innerHTML = `<div class="diag-summary"><b>Диагностика — ${escapeHtml(state.region)}</b><span>сайт ${summary.shown ?? items.length} · с цена ${summary.prices ?? '—'} · приоритетни ${summary.deals ?? '—'} · email кандидати ${summary.alerts ?? '—'} · изтекли ${summary.expired ?? '—'}</span></div>${sourceRows}`;
  } else diagBox.hidden = true;

  const errors = state.payload?.source_errors || [];
  const regionalErrors = errors.filter(e => String(e).includes(state.region) || (state.region === 'Балчик' && String(e).includes('Камара на ЧСИ / Балчик')));
  const warning = document.querySelector('#sourceWarning');
  if(regionalErrors.length){
    warning.hidden = false;
    warning.innerHTML = `<strong>Частичен режим:</strong> ${regionalErrors.map(escapeHtml).join(' · ')}. Останалите източници за ${escapeHtml(state.region)} са обновени.`;
  } else warning.hidden = true;
  render();
}

function setRegion(region){
  if(!['Балчик','Варна'].includes(region) || state.region === region) return;
  state.region = region;
  document.querySelector('#source').value = '';
  document.querySelector('#category').value = '';
  refreshRegionView();
}

async function load(){
  const res = await fetch(`data/listings.json?t=${Date.now()}`, {cache:'no-store'});
  if(!res.ok) throw new Error(`HTTP ${res.status}`);
  state.payload = await res.json();
  refreshRegionView();

  render();
}

document.querySelectorAll('input,select').forEach(el => el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', render));
document.querySelectorAll('.region-tab').forEach(btn => btn.addEventListener('click', () => setRegion(btn.dataset.region)));

document.addEventListener('change', (ev) => {
  const cb = ev.target.closest?.('.review-toggle');
  if(!cb) return;
  const item = (state.payload?.items || []).find(x => listingKey(x) === cb.dataset.reviewKey);
  if(!item) return;
  setReviewed(item, cb.checked);
  const reviewedCount = document.querySelector('#reviewedCount');
  if(reviewedCount) reviewedCount.textContent = regionItems().filter(isReviewed).length;
  render();
});
load().catch(err => {
  document.querySelector('#cards').innerHTML = `<div class="empty">Не успях да заредя данните: ${escapeHtml(String(err))}</div>`;
});
