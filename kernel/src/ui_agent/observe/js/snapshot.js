// ui_agent 页面快照：索引化可交互元素 + 可见文本 + 视口/滚动信息。
// 由 Python 侧逐帧 evaluate 调用，args = {start, maxElements, maxText}。
// 索引标记 data-uiagent-idx 由本脚本写入；选择器永远只存在于 executor 内部。
(args) => {
  if (!document.body) return null;
  const MARK = 'data-uiagent-idx';
  const start = args.start, maxElements = args.maxElements, maxText = args.maxText;

  // 所有观察根：文档 + 各级 open shadow root（closed root 从页面 JS 无法进入，只能放弃）
  const roots = () => {
    const out = [document];
    for (let i = 0; i < out.length; i++) {
      for (const e of out[i].querySelectorAll('*')) if (e.shadowRoot) out.push(e.shadowRoot);
    }
    return out;
  };
  // 祖先链：跨 shadow 边界（ShadowRoot 的 parentNode 是 null，往上要经由 host）
  const chain = e => {
    const out = [];
    for (let n = e; n; n = n.parentNode || n.host || null) out.push(n);
    return out;
  };
  const hiddenAncestor = e => chain(e).some(n => n.nodeType === 1 &&
    (n.getAttribute('aria-hidden') === 'true' || n.hasAttribute('inert')));

  for (const root of roots()) {
    root.querySelectorAll('[' + MARK + ']').forEach(e => e.removeAttribute(MARK));
  }

  const visible = e => !hiddenAncestor(e) &&
    e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});

  const isPassword = e => e.type === 'password';
  const isFile = e => e.tagName === 'INPUT' && e.type === 'file';
  // 隐藏的文件输入自己没有文字：名字只能来自最近一层有文字的祖先（按钮文案、拖拽区提示）
  const fileLabel = e => {
    const own = nameOf(e);
    if (own.trim()) return own.trim();
    let p = e.parentElement;
    for (let i = 0; p && i < 4; i++, p = p.parentElement) {
      const text = (p.innerText || '').replace(/\s+/g, ' ').trim();
      if (text) return text.length > 120 ? text.slice(0, 120) + '…' : text;
    }
    return '文件上传';
  };

  const roles = ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemradio',
    'option', 'gridcell', 'combobox', 'textbox', 'searchbox', 'spinbutton'];
  const selector = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],' +
    roles.map(r => '[role="' + r + '"]').join(',');

  const roleOf = e => {
    const explicit = e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName === 'BUTTON' || e.tagName === 'SUMMARY') return 'button';
    if (e.tagName === 'A') return 'link';
    if (e.tagName === 'SELECT') return 'combobox';
    if (e.tagName === 'TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName === 'INPUT') {
      if (e.type === 'file') return 'file';
      if (['checkbox', 'radio'].includes(e.type)) return e.type;
      if (['button', 'submit', 'reset', 'image'].includes(e.type)) return 'button';
      if (e.type === 'search') return 'searchbox';
      if (e.type === 'number') return 'spinbutton';
      if (['text', 'email', 'url', 'tel', 'password'].includes(e.type)) return 'textbox';
    }
    return null;
  };

  const nameOf = (e, seen = new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const root = e.getRootNode();
    const byId = id => (root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
    const referenced = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => nameOf(byId(id), seen)).filter(Boolean).join(' ');
    // Ant Design v3 下拉：优先已选值/占位文本，避免 "占位 + 值 + 清除/箭头图标" 混在一起
    // （占位元素仍留在 DOM 里但 display:none，textContent 会把它捡回来）。
    if (e.getAttribute('role') === 'combobox') {
      const v = e.querySelector('.ant-select-selection-selected-value');
      const t = v ? (v.getAttribute('title') || v.textContent || '') :
        (e.querySelector('.ant-select-selection__placeholder, .ant-select-selection__rendered')?.textContent || '');
      if (t.trim()) return t.trim();
    }
    return referenced || e.getAttribute('aria-label') ||
      [...(e.labels || [])].map(l => nameOf(l, seen)).filter(Boolean).join(' ') ||
      (['button', 'submit', 'reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName === 'INPUT' ? '' : [...e.childNodes].map(n => n.nodeType === 3 ? n.textContent :
        n.nodeType === 1 && n.getAttribute('aria-hidden') !== 'true' ? nameOf(n, seen) : '').join(' ').trim()) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '';
  };

  // 命中测试：先问文档，命中 shadow 宿主就进它的 shadowRoot 继续问，直到最深处。
  // 不能直接对元素自己的 root 提问：那样看不见盖在宿主上面的 light DOM 元素。
  const deepAt = (x, y) => {
    let node = document.elementFromPoint(x, y);
    while (node && node.shadowRoot) {
      const inner = node.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === node) break;
      node = inner;
    }
    return node;
  };
  const occluderAt = (e, x, y) => {
    const top = deepAt(x, y);
    if (!top || top === e || e.contains(top) || top.contains(e) || chain(e).includes(top)) return '';
    return (nameOf(top) || top.tagName || '').slice(0, 60);
  };

  const elements = [];
  const candidates = [];
  for (const root of roots()) {
    for (const e of root.querySelectorAll(selector)) candidates.push(e);
  }
  for (const e of candidates) {
    if (elements.length >= maxElements) break;
    if (e.type === 'hidden') continue;
    const fileEl = isFile(e);
    const rname = roleOf(e);
    if (!rname) continue;
    const r = e.getBoundingClientRect();
    const onScreen = r.width > 0 && r.height > 0 && visible(e);
    // 文件输入常被 display:none 藏起来（点按钮/点框才是用户入口）：仍然暴露，UPLOAD 不依赖可见
    if (!onScreen && !fileEl) continue;

    const inViewport = onScreen && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    const disabled = e.matches(':disabled') || chain(e).some(n => n.nodeType === 1 &&
      n.getAttribute('aria-disabled') === 'true');
    const x = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1);
    const y = Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1);
    const occluded = inViewport ? occluderAt(e, x, y) : '';

    const index = start + elements.length + 1;
    e.setAttribute(MARK, String(index));
    const item = {
      index, role: rname, label: (fileEl ? fileLabel(e) : (nameOf(e) || rname)).slice(0, 200),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      in_viewport: inViewport, occluded_by: occluded, disabled, operations: [],
    };
    if (e.getRootNode().host) item.shadow = true;
    // 下拉框给"人看到的选中项文案"，原始 value 留在 options 里：验收判据要能直接对账
    if (fileEl) {
      // 文件路径（C:\fakepath\…）不进任何模型输入；给"已选几个"这个事实
      item.value = e.files && e.files.length ? e.files.length + ' 个文件' : '';
      item.accept = (e.getAttribute('accept') || '').slice(0, 200);
      item.multiple = e.multiple === true;
      if (!onScreen) item.hidden = true;
    } else if (e.tagName === 'SELECT') item.value = [...e.selectedOptions].map(o => (o.label || o.text || '').trim()).join(', ');
    else if (isPassword(e)) { item.secret = true; item.value = e.value ? '\u2022\u2022\u2022\u2022\u2022\u2022' : ''; }
    else if ('value' in e) item.value = String(e.value ?? '').slice(0, 300);
    else if (e.isContentEditable) item.value = e.innerText.trim().slice(0, 300);
    for (const key of ['checked', 'selected', 'expanded']) {
      const v = e.getAttribute('aria-' + key);
      if (v !== null) item[key] = v;
    }
    if (['checkbox', 'radio'].includes(e.type)) item.checked = String(e.checked);

    if (fileEl) {
      if (!disabled) item.operations.push('UPLOAD');
    } else if (!disabled) {
      item.operations.push('CLICK', 'HOVER');
      const editable = !e.readOnly && e.getAttribute('aria-readonly') !== 'true' &&
        (['textbox', 'searchbox', 'spinbutton'].includes(rname) ||
          (rname === 'combobox' && ['INPUT', 'TEXTAREA'].includes(e.tagName)) || e.isContentEditable);
      if (editable) item.operations.push('TYPE_TEXT');
      if (e.tagName === 'SELECT') {
        item.operations.push('SELECT');
        item.options = [...e.options].map((o, i) => ({
          key: index + ':' + (i + 1), dom_index: i,
          label: String(o.label || o.text || '').slice(0, 120), value: o.value,
          selected: o.selected, disabled: o.disabled,
        }));
      }
    }
    elements.push(item);
  }

  // 文本：视口内优先，其次整页可见文本，合计不超过 maxText；shadow 树里的文字同样要收
  const inView = [], rest = [];
  const range = document.createRange();
  for (const root of roots()) {
    const walker = document.createTreeWalker(root === document ? document.body : root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const value = node.textContent.trim(), parent = node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node);
      const r = range.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const inside = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
      (inside ? inView : rest).push(value);
    }
  }
  const words = [];
  let total = 0;
  for (const w of inView.concat(rest)) {
    if (total >= maxText) break;
    words.push(w); total += w.length + 1;
  }
  const text = words.join('\n').slice(0, maxText);
  const doc = document.documentElement;

  const fingerprint = JSON.stringify([
    location.href, Math.round(scrollX), Math.round(scrollY), innerWidth, innerHeight,
    document.title, text.length,
    elements.map(e => [e.index, e.role, e.label, e.value ?? '', e.checked ?? '', e.selected ?? '', e.occluded_by]),
  ]);

  return {
    frame_url: location.href,
    page: {
      url: location.href, title: document.title, text,
      viewport: { w: innerWidth, h: innerHeight, dpr: devicePixelRatio },
      scroll: { y: Math.round(scrollY), max: Math.max(0, doc.scrollHeight - innerHeight) },
    },
    elements, fingerprint,
  };
}
