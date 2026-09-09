/* Shared desktop/mobile transfer dialog; opening a composer is not dispatch. */
(function(root) {
  "use strict";
  let active = null;
  function close() { if (active) active.remove(); active = null; }
  function element(tag, text, parent) {
    const el = document.createElement(tag);
    if (text != null) el.textContent = text;
    if (parent) parent.appendChild(el);
    return el;
  }
  async function open(options) {
    close();
    const overlay = element("div", null, document.body);
    active = overlay;
    overlay.style.cssText = "position:fixed;inset:0;background:#0009;z-index:11000;display:flex;align-items:center;justify-content:center;padding:16px";
    const panel = element("section", null, overlay);
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", "新建并接续");
    panel.style.cssText = "box-sizing:border-box;width:540px;max-width:100%;max-height:90dvh;overflow:auto;padding:22px;border:1px solid #7776;border-radius:16px;background:var(--bg-2,#fff);color:var(--text,#1f2430);box-shadow:0 20px 80px #0008";
    element("h3", "新建并接续", panel).style.margin = "0 0 12px";
    element("div", options.title || "", panel).style.cssText = "opacity:.7;overflow-wrap:anywhere;margin-bottom:12px";
    const content = element("div", null, panel);
    const status = element("p", "正在准备…", panel);
    status.setAttribute("role", "status");
    status.style.cssText = "font-size:13px;line-height:1.7;overflow-wrap:anywhere";
    const actions = element("div", null, panel);
    actions.style.cssText = "display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap";
    const button = (label, action) => {
      const b = element("button", label, actions);
      b.type = "button";
      b.style.cssText = "padding:9px 14px;border:1px solid #8886;border-radius:8px;background:var(--bg-3,#eef0f6);color:inherit;cursor:pointer";
      b.onclick = action;
      return b;
    };
    button("取消", close);
    overlay.onclick = e => { if (e.target === overlay) close(); };
    overlay.onkeydown = e => { if (e.key === "Escape") close(); };
    try {
      if (options.native) {
        const result = await options.prepare();
        if (active !== overlay) return;
        if (!result || !result.ok) throw new Error(result && result.error || "准备接续失败");
        const text = element("textarea", null, content);
        text.setAttribute("aria-label", "接续内容");
        text.readOnly = true;
        text.value = result.prompt || "";
        text.style.cssText = "box-sizing:border-box;width:100%;height:220px;padding:10px;resize:vertical;background:transparent;color:inherit;border:1px solid #8886;border-radius:8px";
        status.textContent = "已准备接续内容，来源任务仍保留。在 Codex 新任务中选择模型和思考强度后发送；也可复制内容，在当前客户端新建任务后粘贴。";
        button("复制接续内容", async () => {
          await options.copy(result.prompt);
        });
        button("打开 Codex", async () => {
          try {
            if (options.openLink) {
              const opened = await options.openLink(result.url);
              if (!opened || !opened.ok) throw new Error(opened && opened.error || "打开失败");
            } else {
              const a = element("a", null, panel);
              a.href = result.url;
              a.click(); a.remove();
            }
            status.textContent = "已请求打开 Codex。请在新任务中选择模型后发送；若此设备未安装 Codex，可使用复制接续内容。";
          } catch (e) { status.textContent = e.message; }
        });
      } else {
        const label = element("label", "新任务名称", content);
        label.style.display = "block";
        const name = element("input", null, label);
        name.setAttribute("aria-label", "新任务名称");
        name.value = options.name || "";
        name.style.cssText = "box-sizing:border-box;width:100%;margin:6px 0 12px;padding:9px;background:transparent;color:inherit;border:1px solid #8886;border-radius:8px";
        const windows = element("select", null, content);
        windows.setAttribute("aria-label", "目标窗口");
        windows.style.cssText = "width:100%;padding:9px;background:var(--bg-3,#eef0f6);color:inherit;border:1px solid #8886;border-radius:8px";
        (options.instances || []).forEach(it => windows.add(new Option(it.label || it.id, it.id)));
        if (options.instance) windows.value = options.instance;
        const modelBox = element("div", null, content);
        let catalog;
        try { catalog = await options.models(); } catch (_) { catalog = null; }
        if (active !== overlay) return;
        const picker = root.RxyyMcpModelPicker.mount(modelBox, catalog && catalog.models || []);
        status.textContent = catalog && catalog.ok
          ? "选择模型与参数后点击创建。接手单会在新对话报到后派发。"
          : "模型目录暂不可用，将沿用目标窗口当前模型。";
        const go = button("创建并接续", async () => {
          if (go.disabled) return;
          go.disabled = true;
          try {
            const result = await options.create({name: name.value.trim(), instance: windows.value, model: picker.selected()});
            if (!result || !result.ok) throw new Error(result && result.error || "创建失败");
            status.textContent = "创建命令已发出，等待新对话报到后派发接手单。";
            go.textContent = "已发出";
            if (options.onCreated) await options.onCreated(result);
          } catch (e) { status.textContent = e.message; go.disabled = false; }
        });
      }
      actions.querySelector("button").focus();
    } catch (e) { if (active === overlay) status.textContent = e.message; }
  }
  function openNew(options) {
    close();
    const overlay = element("div", null, document.body);
    active = overlay;
    overlay.style.cssText = "position:fixed;inset:0;background:#0008;z-index:10050;display:flex;align-items:center;justify-content:center;padding:18px";
    const panel = element("div", null, overlay);
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", "新建 Codex 任务");
    panel.style.cssText = "box-sizing:border-box;width:600px;max-width:100%;max-height:90vh;overflow:auto;padding:24px;border-radius:16px;background:var(--bg-2,#fff);color:var(--text,#1f2430);box-shadow:0 20px 80px #0008";
    element("h3", "新建 Codex 任务", panel);
    const projectLabel = element("label", "项目目录", panel);
    projectLabel.style.display = "block";
    const project = element("input", null, projectLabel);
    project.setAttribute("aria-label", "项目目录");
    project.setAttribute("list", "nativeNewProjects"); project.value = options.project || "";
    const list = element("datalist", null, panel); list.id = "nativeNewProjects";
    [...new Set(options.projects || [])].filter(Boolean).forEach(path => {
      const item = element("option", null, list); item.value = path;
    });
    project.style.cssText = "box-sizing:border-box;width:100%;margin:8px 0 14px;padding:10px;background:transparent;color:inherit;border:1px solid #8886;border-radius:8px";
    const promptLabel = element("label", "任务内容（可留空）", panel);
    const prompt = element("textarea", null, promptLabel);
    prompt.setAttribute("aria-label", "任务内容（可留空）"); prompt.maxLength = 24000;
    prompt.style.cssText = "box-sizing:border-box;width:100%;height:140px;margin-top:8px;padding:10px;background:transparent;color:inherit;border:1px solid #8886;border-radius:8px";
    const modelLabel = element("label", "模型", panel);
    modelLabel.style.cssText = "display:block;margin-top:14px";
    const model = element("select", null, modelLabel);
    model.setAttribute("aria-label", "Codex 模型");
    model.style.cssText = "box-sizing:border-box;width:100%;margin-top:8px;padding:10px;background:var(--bg-3,#eef0f6);color:inherit;border:1px solid #8886;border-radius:8px";
    const effortLabel = element("label", "思考程度", panel);
    effortLabel.style.cssText = "display:block;margin-top:14px";
    const effort = element("select", null, effortLabel);
    effort.setAttribute("aria-label", "Codex 思考程度");
    effort.style.cssText = model.style.cssText;
    const status = element("p", "正在读取 Codex 模型目录…", panel);
    status.setAttribute("role", "status"); status.style.cssText = "font-size:13px;line-height:1.7";
    const actions = element("div", null, panel); actions.style.cssText = "display:flex;gap:10px;justify-content:flex-end";
    const cancel = element("button", "取消", actions); cancel.onclick = close;
    const go = element("button", "创建空任务", actions);
    for (const b of [cancel, go]) { b.type = "button"; b.style.cssText = "padding:9px 14px;border:1px solid #8886;border-radius:8px;background:var(--bg-3,#eef0f6);color:inherit;cursor:pointer"; }
    go.disabled = true;
    let creationLocked = false;
    const lockCreation = locked => {
      creationLocked = locked;
      go.disabled = locked;
      for (const field of [project, prompt, model, effort]) field.disabled = locked;
    };
    const syncActionLabel = () => { go.textContent = prompt.value.trim() ? "创建并发送" : "创建空任务"; };
    prompt.addEventListener("input", syncActionLabel);
    let entries = [];
    let modelTouched = false;
    let effortTouched = false;
    let loadSequence = 0;
    const updateReadyStatus = () => {
      const selected = entries.find(x => x.model === model.value);
      if (!selected) return;
      status.textContent = "当前选择 " + (selected.label || selected.model) + " / " + effort.value +
        "。任务内容不为空时，点击后会直接发送并开始；留空则只创建已配置的空白任务。";
    };
    const renderEfforts = preferredValue => {
      effort.replaceChildren();
      const item = entries.find(x => x.model === model.value);
      (item && item.efforts || []).forEach(value => effort.add(new Option(value, value)));
      const preferred = preferredValue || item && item.default_effort;
      if (preferred && [...effort.options].some(x => x.value === preferred)) effort.value = preferred;
    };
    model.onchange = () => {
      modelTouched = true; effortTouched = false; renderEfforts(); updateReadyStatus();
    };
    effort.onchange = () => { effortTouched = true; updateReadyStatus(); };
    const loadModels = async () => {
      if (creationLocked) return;
      const sequence = ++loadSequence;
      const previousModel = model.value;
      const previousEffort = effort.value;
      go.disabled = true;
      status.textContent = "正在读取该项目的 Codex 模型配置…";
      try {
        const result = await options.models(project.value.trim());
        if (active !== overlay || sequence !== loadSequence || creationLocked) return;
        if (!result || !result.ok || !(result.models || []).length) {
          throw new Error(result && result.error || "Codex 模型目录暂不可用");
        }
        entries = result.models.filter(x => x && x.model && (x.efforts || []).length);
        if (!entries.length) throw new Error("Codex 没有可用于新任务的模型");
        model.replaceChildren();
        entries.forEach(item => model.add(new Option(item.label || item.model, item.model)));
        const explicitModel = modelTouched && entries.find(x => x.model === previousModel);
        const selected = explicitModel || entries.find(x => x.model === result.default_model) || entries[0];
        model.value = selected.model;
        const explicitEffort = effortTouched && selected.efforts.includes(previousEffort)
          ? previousEffort : "";
        const effectiveEffort = explicitEffort || (!explicitModel ? result.default_effort : "") ||
          selected.default_effort;
        renderEfforts(effectiveEffort);
        go.disabled = false;
        updateReadyStatus();
      } catch (e) {
        if (active === overlay && sequence === loadSequence) status.textContent = e.message;
      }
    };
    project.addEventListener("change", loadModels);
    Promise.resolve().then(loadModels);
    go.onclick = async () => {
      if (go.disabled || creationLocked) return;
      lockCreation(true);
      try {
        status.textContent = "正在创建并打开 Codex 任务…";
        const result = await options.create(project.value.trim(), prompt.value, model.value, effort.value);
        if (active !== overlay) return;
        if (result && result.session_id && typeof options.onCreated === "function") {
          try { await options.onCreated(result); } catch (_) {}
        }
        if (result && result.created && !result.ok) {
          status.textContent = (result.error || "任务已创建，但后续状态未确认") +
            (result.thread_id ? "；任务 ID：" + result.thread_id : "");
          go.textContent = "请到 Codex 核对";
          return;
        }
        if (!result || typeof result !== "object" || typeof result.ok !== "boolean") {
          status.textContent = "创建结果尚未确认，请先核对任务列表或 Codex；为避免重复创建，本按钮已锁定。";
          go.textContent = "请先核对任务列表";
          return;
        }
        if (!result.ok) {
          status.textContent = result.error || "创建失败";
          lockCreation(false);
          syncActionLabel();
          return;
        }
        if (result.warning) {
          status.textContent = "任务已创建并打开，但" + result.warning;
          go.textContent = result.delivery_unknown ? "请到 Codex 核对" : "内容未发送";
        } else if (result.dispatched) {
          status.textContent = "已按 " + (result.model || model.value) + " / " +
            (result.effort || effort.value) + " 直接开始任务。";
          go.textContent = "任务已开始";
        } else {
          status.textContent = "已创建并打开空白任务，模型和思考程度已经保存。";
          go.textContent = "任务已创建";
        }
      } catch (e) {
        if (active !== overlay) return;
        status.textContent = "创建结果尚未确认，请先核对任务列表或 Codex；为避免重复创建，本按钮已锁定。";
        go.textContent = "请先核对任务列表";
      }
    };
    overlay.onclick = e => { if (e.target === overlay) close(); };
    overlay.onkeydown = e => { if (e.key === "Escape") close(); };
    project.focus();
  }
  root.RxyyMcpTransferDialog = {open: open, openNew: openNew, close: close};
  root.ChijiuTransferDialog = root.RxyyMcpTransferDialog; // legacy cached pages
})(typeof window === "undefined" ? globalThis : window);
