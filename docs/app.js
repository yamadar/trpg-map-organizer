// TRPG Map Organizer - 静的サイト用クライアント
//
// data/maps.json を読み込み、グリッド表示・タグフィルタ・全文検索・モーダル
// プレビューを提供する。フィルタ状態は URL のハッシュに同期され、リンクで共有
// できる。

const CATEGORIES = ['terrain', 'mood', 'location'];

const state = {
  data: null,
  query: '',
  mode: 'any',
  selected: { terrain: new Set(), mood: new Set(), location: new Set() },
};

async function init() {
  try {
    const res = await fetch('data/maps.json', { cache: 'no-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
  } catch (e) {
    document.getElementById('grid').innerHTML =
      `<p class="loading">データの読み込みに失敗しました: ${e.message}</p>`;
    return;
  }

  renderFilters();
  applyHash();
  bindEvents();
  render();

  const gen = state.data.generated_at;
  if (gen) document.getElementById('generated-at').textContent = `生成: ${gen}`;
}

function renderFilters() {
  for (const cat of CATEGORIES) {
    const tags = state.data.tags[cat] || [];
    const container = document.getElementById(cat);
    container.innerHTML = '';
    for (const tag of tags) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip';
      btn.textContent = tag;
      btn.dataset.cat = cat;
      btn.dataset.tag = tag;
      btn.addEventListener('click', () => toggleTag(cat, tag, btn));
      container.appendChild(btn);
    }
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

function filterMaps() {
  const q = state.query.trim().toLowerCase();
  return state.data.maps.filter(m => {
    if (q && !m.file.toLowerCase().includes(q)) return false;

    for (const cat of CATEGORIES) {
      const sel = state.selected[cat];
      if (sel.size === 0) continue;

      const have = new Set(m[cat] || []);
      if (state.mode === 'all') {
        for (const t of sel) {
          if (!have.has(t)) return false;
        }
      } else {
        let any = false;
        for (const t of sel) {
          if (have.has(t)) { any = true; break; }
        }
        if (!any) return false;
      }
    }
    return true;
  });
}

function render() {
  const filtered = filterMaps();
  const grid = document.getElementById('grid');
  const count = document.getElementById('count');

  count.textContent = `${filtered.length} / ${state.data.maps.length} 件`;

  if (filtered.length === 0) {
    grid.innerHTML = '<p class="loading">条件に一致するマップはありません。</p>';
    return;
  }

  const frag = document.createDocumentFragment();
  for (const m of filtered) {
    const card = document.createElement('article');
    card.className = 'card';
    card.dataset.id = m.id;
    card.innerHTML = `
      <img loading="lazy" src="images/thumb/${escapeAttr(m.thumb)}" alt="${escapeAttr(m.file)}">
      <div class="card-body">
        <p class="card-title">${escapeHtml(stripExt(m.file))}</p>
        <p class="card-tags">${summary(m)}</p>
      </div>
    `;
    card.addEventListener('click', () => showPreview(m));
    frag.appendChild(card);
  }
  grid.innerHTML = '';
  grid.appendChild(frag);
}

function summary(m) {
  const parts = [];
  for (const cat of CATEGORIES) {
    const xs = (m[cat] || []).slice(0, 3);
    if (xs.length === 0) continue;
    parts.push(xs.map(t => `<code>${escapeHtml(t)}</code>`).join(''));
  }
  return parts.join(' ');
}

function showPreview(m) {
  document.getElementById('preview-img').src = `images/mid/${m.mid}`;
  document.getElementById('preview-img').alt = m.file;
  document.getElementById('preview-title').textContent = m.file;
  document.getElementById('preview-desc').textContent = m.desc || '';
  for (const cat of CATEGORIES) {
    const el = document.getElementById(`preview-${cat}`);
    el.innerHTML = (m[cat] || [])
      .map(t => `<code>${escapeHtml(t)}</code>`)
      .join('');
  }
  const dlg = document.getElementById('preview');
  if (!dlg.open) dlg.showModal();
}

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

  document.getElementById('close').addEventListener('click', () => {
    document.getElementById('preview').close();
  });

  document.getElementById('preview').addEventListener('click', e => {
    // 背景クリックで閉じる
    if (e.target.id === 'preview') {
      document.getElementById('preview').close();
    }
  });

  window.addEventListener('hashchange', () => {
    applyHash();
    render();
  });
}

function saveHash() {
  const params = new URLSearchParams();
  if (state.query) params.set('q', state.query);
  if (state.mode !== 'any') params.set('m', state.mode);
  for (const cat of CATEGORIES) {
    if (state.selected[cat].size > 0) {
      params.set(cat[0], [...state.selected[cat]].join(','));
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

  state.mode = params.get('m') === 'all' ? 'all' : 'any';
  document.getElementById('match-mode').value = state.mode;

  for (const cat of CATEGORIES) {
    const raw = params.get(cat[0]) || '';
    const tags = raw ? raw.split(',') : [];
    state.selected[cat] = new Set(tags);
    updateBadge(cat);
    // チップの active 状態を同期
    for (const btn of document.querySelectorAll(`.chip[data-cat="${cat}"]`)) {
      btn.classList.toggle('active', state.selected[cat].has(btn.dataset.tag));
    }
  }
}

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
