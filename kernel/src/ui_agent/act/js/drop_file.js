// C 方案（保真拖拽）：在页内构造 DataTransfer，派发 dragenter/dragover/drop。
// 节点由索引查出、文件字节由 Python 侧传入——脚本本身是仓库固定代码，不是模型输出。
// 已知边界：事件 isTrusted=false，个别库会忽略非可信事件（那种页面只能用 A 方案注入 input）。
(args) => {
  // 观察根：文档 + 各级 open shadow root，索引可能落在其中任意一个里
  const roots = () => {
    const out = [document];
    for (let i = 0; i < out.length; i++) {
      for (const e of out[i].querySelectorAll('*')) if (e.shadowRoot) out.push(e.shadowRoot);
    }
    return out;
  };
  const want = '[data-uiagent-idx="' + args.index + '"]';
  let el = null;
  for (const root of roots()) {
    el = root.querySelector(want);
    if (el) break;
  }
  if (!el) return {ok: false, reason: '元素不在 DOM 中'};
  const bytes = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  let dt;
  try {
    dt = new DataTransfer();
    for (const f of args.files) dt.items.add(new File([bytes(f.b64)], f.name, {type: f.type}));
  } catch (exc) {
    return {ok: false, reason: '构造 DataTransfer 失败：' + exc.message};
  }
  if (dt.files.length !== args.files.length) {
    return {ok: false, reason: '文件没有被放进 DataTransfer'};
  }
  const opts = {bubbles: true, cancelable: true, composed: true, dataTransfer: dt};
  // 拖拽区常要求先 dragover.preventDefault() 才算有效目标；合成事件照样按真实顺序派发
  el.dispatchEvent(new DragEvent('dragenter', opts));
  el.dispatchEvent(new DragEvent('dragover', opts));
  el.dispatchEvent(new DragEvent('drop', opts));
  return {ok: true, files: dt.files.length};
}
