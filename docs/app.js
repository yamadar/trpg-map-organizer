// TRPG Map Organizer - 静的サイト用クライアント
//
// 機能:
//  - data/maps.json と data/i18n.json を読み込み
//  - i18n (en/ja) を navigator.language で自動選択 + 手動切替
//  - グリッド表示・タグフィルタ・全文検索・URL ハッシュ同期
//  - モーダルプレビュー: Prev/Next、元画像/JPEG ダウンロード、URL コピー
//  - スマホでもスワイプとボタンで操作できる

// 表示順 = 内部順。theme を先頭に置く。
const CATEGORIES = ['theme', 'terrain', 'mood', 'location'];

// URL ハッシュ用の 1〜2 文字キー (g=genre=theme で衝突回避)
const HASH_KEYS = { theme: 'g', terrain: 't', mood: 'm', location: 'l' };

// プレビューで前後何件をブラウザキャッシュに先読みするか
const PREFETCH_RADIUS = 2;

const _MOBILE_MQ = window.matchMedia('(max-width: 800px)');

const state = {
  data: null,
  i18n: null,
  lang: 'ja',
  query: '',
  mode: 'any',
  selected: {
    theme: new Set(),
    terrain: new Set(),
    mood: new Set(),
    location: new Set(),
  },
  filtered: [],
  previewIndex: -1,
  previewId: null,        // URL ハッシュに反映する map.id
  currentImageSrc: '',    // 現在表示中の画像 URL (二重ロード防止)
};

// ===== 初期化 =====

// i18n.json が無くても UI が壊れないようフォールバック (英語のみのキーフォールバック)
const _I18N_FALLBACK = {
  ui: { en: {}, ja: {} },
  tags: {},
};

async function init() {
  // maps.json は必須。i18n.json は任意 (無くても日本語のラベルが既定値として残る)。
  try {
    const r = await fetch('data/maps.json', { cache: 'no-cache' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    state.data = await r.json();
  } catch (e) {
    const msg = (_I18N_FALLBACK.ui.ja.load_error || 'データの読み込みに失敗しました') +
                ': ' + String(e.message || e);
    document.getElementById('grid').innerHTML =
      `<p class="loading">${escapeHtml(msg)}</p>`;
    return;
  }

  try {
    const r = await fetch('data/i18n.json', { cache: 'no-cache' });
    state.i18n = r.ok ? await r.json() : _I18N_FALLBACK;
  } catch {
    state.i18n = _I18N_FALLBACK;
  }
  // ui.en / ui.ja のどちらかが欠落しても t() が落ちないよう保険を入れる
  state.i18n.ui = state.i18n.ui || {};
  state.i18n.ui.en = state.i18n.ui.en || {};
  state.i18n.ui.ja = state.i18n.ui.ja || state.i18n.ui.en;
  state.i18n.tags = state.i18n.tags || {};

  state.lang = detectLang();
  applyUiTranslations();
  renderFilters();
  applyMobileDefaults();
  applyHash();
  bindEvents();
  render();
  updateFilterToggle();

  // URL に id があれば対応するモーダルを開く (初回表示時のみ)
  if (state.previewId != null) {
    openPreviewById(state.previewId, { fromHash: true });
  }

  const gen = state.data.generated_at;
  if (gen) document.getElementById('generated-at').textContent = `${gen}`;
}

// モバイル時のサイドバー内デフォルト: theme のみ展開、それ以外は折り畳み
function applyMobileDefaults() {
  if (!_MOBILE_MQ.matches) return;
  for (const cat of CATEGORIES) {
    if (cat === 'theme') continue;
    const det = document.getElementById(cat)?.closest('details');
    if (det) det.open = false;
  }
}

function detectLang() {
  // localStorage の手動設定が最優先
  const saved = localStorage.getItem('lang');
  if (saved === 'en' || saved === 'ja') return saved;
  // navigator.language で判定 (ja 系なら ja、それ以外 en)
  const nav = (navigator.language || navigator.userLanguage || 'en').toLowerCase();
  return nav.startsWith('ja') ? 'ja' : 'en';
}

function t(key) {
  const ui = state.i18n.ui[state.lang] || state.i18n.ui.en;
  return ui[key] || key;
}

function tagLabel(tag) {
  if (state.lang === 'ja') return tag;
  const en = state.i18n.tags && state.i18n.tags[tag];
  return en || tag;
}

function applyUiTranslations() {
  // textContent
  for (const el of document.querySelectorAll('[data-i18n]')) {
    el.textContent = t(el.dataset.i18n);
  }
  // 属性 (例: data-i18n-attr="placeholder:search_placeholder,title:foo")
  // 不正な記法は警告ログを出してスキップし、他要素の翻訳を止めないようにする
  for (const el of document.querySelectorAll('[data-i18n-attr]')) {
    for (const pair of el.dataset.i18nAttr.split(',')) {
      const idx = pair.indexOf(':');
      if (idx < 1 || idx === pair.length - 1) {
        console.warn('skip malformed data-i18n-attr pair:', pair);
        continue;
      }
      const attr = pair.slice(0, idx).trim();
      const key = pair.slice(idx + 1).trim();
      if (attr && key) el.setAttribute(attr, t(key));
    }
  }
  // html lang 属性
  document.documentElement.lang = state.lang;
  // 言語スイッチャの active 状態
  for (const btn of document.querySelectorAll('.lang-btn')) {
    btn.classList.toggle('active', btn.dataset.lang === state.lang);
  }
}

// ===== フィルタ UI =====

function renderFilters() {
  for (const cat of CATEGORIES) {
    const tags = (state.data.tags && state.data.tags[cat]) || [];
    const container = document.getElementById(cat);
    container.innerHTML = '';
    for (const tag of tags) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip';
      btn.dataset.cat = cat;
      btn.dataset.tag = tag;
      btn.textContent = tagLabel(tag);
      btn.addEventListener('click', () => toggleTag(cat, tag, btn));
      container.appendChild(btn);
    }
    updateBadge(cat);
  }
}

function refreshChipLabels() {
  // 言語切替時にチップのテキストを更新
  for (const btn of document.querySelectorAll('.chip')) {
    btn.textContent = tagLabel(btn.dataset.tag);
  }
}

function toggleTag(cat, tag, btn) {
  const set = state.selected[cat];
  if (set.has(tag)) {
    set.delete(tag);
    btn.classList.remove('active');
  } else {
    set.add(tag);
    btn.classList.add('active');
  }
  updateBadge(cat);
  updateFilterToggle();
  saveHash();
  render();
}

function updateFilterToggle() {
  const btn = document.getElementById('filter-toggle');
  if (!btn) return;
  const total = CATEGORIES.reduce((s, c) => s + state.selected[c].size, 0);
  const hasFilters = total > 0 || state.query.length > 0 || state.mode !== 'any';
  btn.classList.toggle('has-filters', hasFilters);
  document.getElementById('filter-badge').textContent = total;
}

function openSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('sidebar-backdrop').classList.add('visible');
  document.getElementById('sidebar-backdrop').hidden = false;
  document.getElementById('filter-toggle').setAttribute('aria-expanded', 'true');
}

function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-backdrop').classList.remove('visible');
  document.getElementById('sidebar-backdrop').hidden = true;
  document.getElementById('filter-toggle').setAttribute('aria-expanded', 'false');
}

function toggleSidebar() {
  if (document.getElementById('sidebar').classList.contains('open')) {
    closeSidebar();
  } else {
    openSidebar();
  }
}

function updateBadge(cat) {
  const badge = document.getElementById(`${cat}-selected`);
  const n = state.selected[cat].size;
  badge.textContent = n;
  badge.classList.toggle('has-selection', n > 0);
}

// ===== フィルタロジック =====

function filterMaps() {
  const q = state.query.trim().toLowerCase();
  return state.data.maps.filter(m => {
    if (q && !m.file.toLowerCase().includes(q)) return false;
    for (const cat of CATEGORIES) {
      const sel = state.selected[cat];
      if (sel.size === 0) continue;
      const have = new Set(m[cat] || []);
      if (state.mode === 'all') {
        for (const t of sel) if (!have.has(t)) return false;
      } else {
        let any = false;
        for (const t of sel) if (have.has(t)) { any = true; break; }
        if (!any) return false;
      }
    }
    return true;
  });
}

// ===== カード描画 =====

function render() {
  state.filtered = filterMaps();
  const grid = document.getElementById('grid');
  const count = document.getElementById('count');

  count.textContent = `${state.filtered.length} / ${state.data.maps.length}${t('count_unit')}`;

  if (state.filtered.length === 0) {
    grid.innerHTML = `<p class="loading">${escapeHtml(t('no_results'))}</p>`;
    return;
  }

  const frag = document.createDocumentFragment();
  state.filtered.forEach((m, idx) => {
    const card = document.createElement('article');
    card.className = 'card';
    card.dataset.idx = idx;
    card.innerHTML = `
      <img loading="lazy" src="images/thumb/${escapeAttr(m.thumb)}" alt="${escapeAttr(m.file)}">
      <div class="card-body">
        <p class="card-title">${escapeHtml(stripExt(m.file))}</p>
        <p class="card-tags">${cardSummary(m)}</p>
      </div>
    `;
    card.addEventListener('click', () => openPreview(idx));
    frag.appendChild(card);
  });
  grid.innerHTML = '';
  grid.appendChild(frag);
}

function cardSummary(m) {
  const parts = [];
  for (const cat of CATEGORIES) {
    const xs = (m[cat] || []).slice(0, 3);
    if (xs.length === 0) continue;
    parts.push(xs.map(tag => `<code>${escapeHtml(tagLabel(tag))}</code>`).join(''));
  }
  return parts.join(' ');
}

// ===== モーダルプレビュー =====

function openPreview(index) {
  state.previewIndex = index;
  const m = state.filtered[index];
  state.previewId = m ? m.id : null;
  renderPreview();
  saveHash();
  const dlg = document.getElementById('preview');
  if (!dlg.open) dlg.showModal();
}

// id から該当マップを探してモーダルを開く。フィルタ外の id でも閲覧可能。
function openPreviewById(id, { fromHash = false } = {}) {
  let idx = state.filtered.findIndex(m => m.id === id);
  if (idx >= 0) {
    state.previewIndex = idx;
    state.previewId = id;
    renderPreview();
  } else {
    // フィルタ外にあるマップ: previewIndex=-1 で navigation を抑止
    const m = state.data.maps.find(mm => mm.id === id);
    if (!m) {
      // 該当 id なし: URL ハッシュを掃除して何もしない
      state.previewId = null;
      if (!fromHash) saveHash();
      return;
    }
    state.previewIndex = -1;
    state.previewId = id;
    renderPreviewOf(m);
  }
  if (!fromHash) saveHash();
  const dlg = document.getElementById('preview');
  if (!dlg.open) dlg.showModal();
}

// 画像読み込み (ローディング表示 + 二重リクエスト防止)
function setPreviewImage(newSrc, fallbackAlt) {
  const imgEl = document.getElementById('preview-img');
  const wrap = document.getElementById('image-wrap');
  imgEl.alt = fallbackAlt || '';

  if (state.currentImageSrc === newSrc &&
      imgEl.complete && imgEl.naturalWidth > 0) {
    // 既に同じ画像が読み込み済み
    wrap.classList.remove('loading');
    return;
  }
  state.currentImageSrc = newSrc;
  wrap.classList.add('loading');

  const loader = new Image();
  loader.onload = () => {
    if (state.currentImageSrc !== newSrc) return; // 既に別の画像へ移動済み
    imgEl.src = newSrc;
    wrap.classList.remove('loading');
  };
  loader.onerror = () => {
    if (state.currentImageSrc !== newSrc) return;
    wrap.classList.remove('loading');
  };
  loader.src = newSrc;
  if (loader.complete && loader.naturalWidth > 0) {
    // キャッシュヒット: onload が走らない場合があるので即座に反映
    imgEl.src = newSrc;
    wrap.classList.remove('loading');
  }
}

// 前後の画像をブラウザキャッシュにプリフェッチ
function prefetchAround(index, radius = PREFETCH_RADIUS) {
  if (index < 0) return;
  for (let d = 1; d <= radius; d++) {
    for (const k of [index + d, index - d]) {
      if (k < 0 || k >= state.filtered.length) continue;
      const m = state.filtered[k];
      if (!m) continue;
      const img = new Image();
      img.src = `images/mid/${m.mid}`;
    }
  }
}

function renderPreview() {
  const m = state.filtered[state.previewIndex];
  if (!m) return;
  renderPreviewOf(m);
  prefetchAround(state.previewIndex);
}

// 個別マップを引数で受け取って描画 (フィルタ外 id 表示用にも使う)
function renderPreviewOf(m) {
  setPreviewImage(`images/mid/${m.mid}`, m.file);
  document.getElementById('preview-title').textContent = m.file;
  document.getElementById('preview-desc').textContent = m.desc || '';

  // タグ表示 (言語切替対応)
  for (const cat of CATEGORIES) {
    const el = document.getElementById(`preview-${cat}`);
    el.innerHTML = (m[cat] || [])
      .map(tag => `<code>${escapeHtml(tagLabel(tag))}</code>`)
      .join('');
  }

  // ダウンロードリンク (元画像は file_level + per-map の両方で存在確認)
  const orig = document.getElementById('download-original');
  const jpeg = document.getElementById('download-jpeg');
  if (state.data.has_originals && m.has_original !== false) {
    orig.hidden = false;
    orig.href = `originals/${encodeURIComponent(m.file)}`;
    orig.setAttribute('download', m.file);
  } else {
    orig.hidden = true;
  }
  jpeg.href = `images/mid/${encodeURIComponent(m.mid)}`;
  jpeg.setAttribute('download', m.mid);

  // 位置インジケータと prev/next の状態
  // previewIndex=-1 (フィルタ外を URL 経由で表示) のときは位置表示・ナビを抑制
  const positionEl = document.getElementById('position');
  if (state.previewIndex < 0) {
    positionEl.textContent = '— / —';
    document.getElementById('prev-btn').disabled = true;
    document.getElementById('next-btn').disabled = true;
  } else {
    positionEl.textContent =
      `${state.previewIndex + 1}${t('position_sep')}${state.filtered.length}`;
    document.getElementById('prev-btn').disabled = state.previewIndex <= 0;
    document.getElementById('next-btn').disabled =
      state.previewIndex >= state.filtered.length - 1;
  }
}

function navigatePreview(delta) {
  if (state.previewIndex < 0) return; // フィルタ外表示中は無効
  const next = state.previewIndex + delta;
  if (next < 0 || next >= state.filtered.length) return;
  state.previewIndex = next;
  const m = state.filtered[next];
  state.previewId = m ? m.id : null;
  renderPreview();
  saveHash();
}

function closePreview() {
  state.previewIndex = -1;
  state.previewId = null;
  const dlg = document.getElementById('preview');
  if (dlg.open) dlg.close();
  saveHash();
}

let _toastTimer = null;
function _showToast(key) {
  const toast = document.getElementById('toast');
  toast.textContent = t(key);
  toast.hidden = false;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { toast.hidden = true; _toastTimer = null; }, 1500);
}

function copyCurrentImageUrl() {
  const m = state.filtered[state.previewIndex];
  if (!m) return;
  const useOriginal = state.data.has_originals && m.has_original !== false;
  const path = useOriginal
    ? `originals/${encodeURIComponent(m.file)}`
    : `images/mid/${encodeURIComponent(m.mid)}`;
  const url = new URL(path, location.href).href;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url)
      .then(() => _showToast('url_copied'))
      .catch(() => fallbackCopy(url, _showToast));
  } else {
    fallbackCopy(url, _showToast);
  }
}

function fallbackCopy(text, showToast) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    showToast(ok ? 'url_copied' : 'url_copy_failed');
  } catch {
    showToast('url_copy_failed');
  }
}

// ===== イベント =====

function bindEvents() {
  document.getElementById('search').addEventListener('input', e => {
    state.query = e.target.value;
    updateFilterToggle();
    saveHash();
    render();
  });

  document.getElementById('match-mode').addEventListener('change', e => {
    state.mode = e.target.value;
    updateFilterToggle();
    saveHash();
    render();
  });

  document.getElementById('clear').addEventListener('click', () => {
    state.query = '';
    document.getElementById('search').value = '';
    state.mode = 'any';
    document.getElementById('match-mode').value = 'any';
    for (const cat of CATEGORIES) {
      state.selected[cat].clear();
      updateBadge(cat);
    }
    document.querySelectorAll('.chip.active').forEach(b => b.classList.remove('active'));
    updateFilterToggle();
    saveHash();
    render();
  });

  document.getElementById('filter-toggle').addEventListener('click', toggleSidebar);
  document.getElementById('sidebar-backdrop').addEventListener('click', closeSidebar);

  for (const btn of document.querySelectorAll('.lang-btn')) {
    btn.addEventListener('click', () => {
      state.lang = btn.dataset.lang;
      localStorage.setItem('lang', state.lang);
      applyUiTranslations();
      refreshChipLabels();
      render();
      if (document.getElementById('preview').open) renderPreview();
    });
  }

  document.getElementById('close').addEventListener('click', closePreview);

  document.getElementById('preview').addEventListener('click', e => {
    if (e.target.id === 'preview') closePreview();
  });
  // dialog の close イベント (ESC キーなど) でも URL から id を消す
  document.getElementById('preview').addEventListener('close', () => {
    if (state.previewId != null) {
      state.previewIndex = -1;
      state.previewId = null;
      saveHash();
    }
  });

  document.getElementById('prev-btn').addEventListener('click', () => navigatePreview(-1));
  document.getElementById('next-btn').addEventListener('click', () => navigatePreview(1));
  document.getElementById('copy-url').addEventListener('click', copyCurrentImageUrl);

  // キーボード矢印
  document.addEventListener('keydown', e => {
    const dlg = document.getElementById('preview');
    if (!dlg.open) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); navigatePreview(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); navigatePreview(1); }
  });

  // タッチスワイプ (モバイル)
  bindSwipe();

  window.addEventListener('hashchange', () => {
    applyHash();
    render();
    updateFilterToggle();
    const dlg = document.getElementById('preview');
    // URL ハッシュ側の状態に従ってモーダルを同期
    if (state.previewId != null) {
      openPreviewById(state.previewId, { fromHash: true });
    } else if (dlg.open) {
      dlg.close();
    }
  });
}

function bindSwipe() {
  const wrap = document.getElementById('image-wrap');
  let startX = null;
  let startY = null;
  let startTime = 0;
  const THRESHOLD = 50;       // px
  const MAX_ANGLE = 0.7;       // dy/dx の上限 (水平判定)

  wrap.addEventListener('touchstart', e => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    startTime = Date.now();
  }, { passive: true });

  wrap.addEventListener('touchend', e => {
    if (startX === null) return;
    const endTouch = (e.changedTouches && e.changedTouches[0]) || null;
    if (!endTouch) { startX = null; return; }
    const dx = endTouch.clientX - startX;
    const dy = endTouch.clientY - startY;
    startX = null;
    if (Date.now() - startTime > 800) return;
    if (Math.abs(dx) < THRESHOLD) return;
    if (Math.abs(dy / dx) > MAX_ANGLE) return;
    navigatePreview(dx < 0 ? 1 : -1);
  }, { passive: true });
}

// ===== URL ハッシュ同期 =====

function saveHash() {
  const params = new URLSearchParams();
  // 注意: m はマッチモード用に予約済み (1 文字キー)。theme=g とすることで衝突回避。
  if (state.query) params.set('q', state.query);
  if (state.mode !== 'any') params.set('mode', state.mode);
  for (const cat of CATEGORIES) {
    if (state.selected[cat].size > 0) {
      params.set(HASH_KEYS[cat], [...state.selected[cat]].join(','));
    }
  }
  if (state.previewId != null) {
    params.set('id', String(state.previewId));
  }
  const hash = params.toString();
  // 空ハッシュは pathname のみに戻して URL バーを綺麗に保つ
  if (hash) {
    history.replaceState(null, '', `#${hash}`);
  } else if (location.hash) {
    history.replaceState(null, '', location.pathname + location.search);
  }
}

function applyHash() {
  const hash = location.hash.replace(/^#/, '');
  const params = new URLSearchParams(hash);

  state.query = params.get('q') || '';
  document.getElementById('search').value = state.query;

  // 後方互換: 旧 URL は m=any/all を使っていたが、現在 m は mood キー。
  // mode が未指定で m の値が 'all'/'any' の場合のみ「旧 mode」として扱う。
  // それ以外は m を素直に mood タグ選択として読む。
  let modeRaw = params.get('mode');
  let mIsLegacyMode = false;
  if (!modeRaw) {
    const legacyM = params.get('m');
    if (legacyM === 'all' || legacyM === 'any') {
      modeRaw = legacyM;
      mIsLegacyMode = true;
    }
  }
  state.mode = modeRaw === 'all' ? 'all' : 'any';
  document.getElementById('match-mode').value = state.mode;

  for (const cat of CATEGORIES) {
    let raw = params.get(HASH_KEYS[cat]) || '';
    // 旧 mode フォールバックで `m=all|any` が消費された場合は mood の解釈をスキップ
    if (cat === 'mood' && mIsLegacyMode) raw = '';
    const tags = raw ? raw.split(',').filter(Boolean) : [];
    state.selected[cat] = new Set(tags);
    updateBadge(cat);
    for (const btn of document.querySelectorAll(`.chip[data-cat="${cat}"]`)) {
      btn.classList.toggle('active', state.selected[cat].has(btn.dataset.tag));
    }
  }

  // モーダル状態 (id=)
  const idStr = params.get('id');
  if (idStr) {
    const idNum = Number(idStr);
    state.previewId = Number.isFinite(idNum) ? idNum : null;
  } else {
    state.previewId = null;
  }
}

// ===== ユーティリティ =====

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[c]);
}
function escapeAttr(s) { return escapeHtml(s); }
function stripExt(name) {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(0, i) : name;
}

init();
