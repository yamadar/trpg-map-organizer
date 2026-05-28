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
};

// ===== 初期化 =====

async function init() {
  try {
    const [maps, i18n] = await Promise.all([
      fetch('data/maps.json', { cache: 'no-cache' }).then(r => {
        if (!r.ok) throw new Error('maps.json: HTTP ' + r.status);
        return r.json();
      }),
      fetch('data/i18n.json', { cache: 'no-cache' }).then(r => {
        if (!r.ok) throw new Error('i18n.json: HTTP ' + r.status);
        return r.json();
      }),
    ]);
    state.data = maps;
    state.i18n = i18n;
  } catch (e) {
    document.getElementById('grid').innerHTML =
      `<p class="loading">${escapeHtml(String(e.message || e))}</p>`;
    return;
  }

  state.lang = detectLang();
  applyUiTranslations();
  renderFilters();
  applyHash();
  bindEvents();
  render();

  const gen = state.data.generated_at;
  if (gen) document.getElementById('generated-at').textContent = `${gen}`;
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
  for (const el of document.querySelectorAll('[data-i18n-attr]')) {
    for (const pair of el.dataset.i18nAttr.split(',')) {
      const [attr, key] = pair.split(':');
      el.setAttribute(attr.trim(), t(key.trim()));
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
  saveHash();
  render();
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
  renderPreview();
  const dlg = document.getElementById('preview');
  if (!dlg.open) dlg.showModal();
}

function renderPreview() {
  const m = state.filtered[state.previewIndex];
  if (!m) return;

  document.getElementById('preview-img').src = `images/mid/${m.mid}`;
  document.getElementById('preview-img').alt = m.file;
  document.getElementById('preview-title').textContent = m.file;
  document.getElementById('preview-desc').textContent = m.desc || '';

  // タグ表示 (言語切替対応)
  for (const cat of CATEGORIES) {
    const el = document.getElementById(`preview-${cat}`);
    el.innerHTML = (m[cat] || [])
      .map(tag => `<code>${escapeHtml(tagLabel(tag))}</code>`)
      .join('');
  }

  // ダウンロードリンク
  const orig = document.getElementById('download-original');
  const jpeg = document.getElementById('download-jpeg');
  if (state.data.has_originals) {
    orig.hidden = false;
    orig.href = `originals/${encodeURIComponent(m.file)}`;
    orig.setAttribute('download', m.file);
  } else {
    orig.hidden = true;
  }
  jpeg.href = `images/mid/${encodeURIComponent(m.mid)}`;
  jpeg.setAttribute('download', m.mid);

  // 位置インジケータと prev/next の状態
  document.getElementById('position').textContent =
    `${state.previewIndex + 1}${t('position_sep')}${state.filtered.length}`;
  document.getElementById('prev-btn').disabled = state.previewIndex <= 0;
  document.getElementById('next-btn').disabled =
    state.previewIndex >= state.filtered.length - 1;
}

function navigatePreview(delta) {
  const next = state.previewIndex + delta;
  if (next < 0 || next >= state.filtered.length) return;
  state.previewIndex = next;
  renderPreview();
}

function copyCurrentImageUrl() {
  const m = state.filtered[state.previewIndex];
  if (!m) return;
  const path = state.data.has_originals
    ? `originals/${encodeURIComponent(m.file)}`
    : `images/mid/${encodeURIComponent(m.mid)}`;
  const url = new URL(path, location.href).href;
  const showToast = (key) => {
    const toast = document.getElementById('toast');
    toast.textContent = t(key);
    toast.hidden = false;
    setTimeout(() => { toast.hidden = true; }, 1500);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url)
      .then(() => showToast('url_copied'))
      .catch(() => fallbackCopy(url, showToast));
  } else {
    fallbackCopy(url, showToast);
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
    saveHash();
    render();
  });

  document.getElementById('match-mode').addEventListener('change', e => {
    state.mode = e.target.value;
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
    saveHash();
    render();
  });

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

  document.getElementById('close').addEventListener('click', () => {
    document.getElementById('preview').close();
  });

  document.getElementById('preview').addEventListener('click', e => {
    if (e.target.id === 'preview') {
      document.getElementById('preview').close();
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
  const hash = params.toString();
  history.replaceState(null, '', hash ? `#${hash}` : ' ');
}

function applyHash() {
  const hash = location.hash.replace(/^#/, '');
  const params = new URLSearchParams(hash);

  state.query = params.get('q') || '';
  document.getElementById('search').value = state.query;

  // 後方互換: 旧 URL は m=any/all を使っていたので mode 未指定なら m を見る
  const modeRaw = params.get('mode') || params.get('m');
  state.mode = modeRaw === 'all' ? 'all' : 'any';
  document.getElementById('match-mode').value = state.mode;

  for (const cat of CATEGORIES) {
    const raw = params.get(HASH_KEYS[cat]) || '';
    const tags = raw ? raw.split(',') : [];
    state.selected[cat] = new Set(tags);
    updateBadge(cat);
    for (const btn of document.querySelectorAll(`.chip[data-cat="${cat}"]`)) {
      btn.classList.toggle('active', state.selected[cat].has(btn.dataset.tag));
    }
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
