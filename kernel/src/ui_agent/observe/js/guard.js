// 执行前守卫：确认元素仍然连着 DOM、可见、可达（命中测试）。
// 返回 {ok, reason, in_viewport, reachable, point, rect}。
// 由 Python 侧在目标所在 frame 内 evaluate，args = {index}。
// 与 snapshot.js 同源：索引可能落在 open shadow 树里，查找与命中测试都要穿透 shadow 边界。
(args) => {
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

  const want = '[data-uiagent-idx="' + args.index + '"]';
  let el = null;
  for (const root of roots()) {
    el = root.querySelector(want);
    if (el) break;
  }
  if (!el) return { ok: false, reason: 'stale' };
  if (!el.isConnected) return { ok: false, reason: 'disconnected' };
  const visible = !chain(el).some(n => n.nodeType === 1 &&
      (n.getAttribute('aria-hidden') === 'true' || n.hasAttribute('inert'))) &&
    el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  const r = el.getBoundingClientRect();
  if (!visible || r.width <= 0 || r.height <= 0) return { ok: false, reason: 'not_visible' };

  const inViewport = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  const x = Math.min(Math.max(r.left + r.width / 2, 1), innerWidth - 1);
  const y = Math.min(Math.max(r.top + r.height / 2, 1), innerHeight - 1);
  const top = deepAt(x, y);
  const reachable = !!(top && (top === el || el.contains(top) || top.contains(el) || chain(el).includes(top)));
  return {
    ok: true, reason: reachable ? '' : 'occluded', in_viewport: inViewport, reachable,
    point: [Math.round(x), Math.round(y)], rect: [r.x, r.y, r.width, r.height],
  };
}
