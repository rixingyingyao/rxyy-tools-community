/* Shared transfer model picker. Options come from the installed model catalog. */
(function(root) {
  "use strict";
  const remembered = new Map();
  function keyOf(model) { return model.key || model.model; }
  function configOf(model) {
    const key = keyOf(model);
    if (!remembered.has(key)) {
      const values = {};
      (model.parameters || []).forEach(p => { if (p && p.id) values[p.id] = String(p.value); });
      remembered.set(key, {max: !!model.max, values: values});
    }
    return remembered.get(key);
  }
  function fieldsOf(model) {
    const schema = model.schema || {};
    const fields = [];
    if (schema.think_id && (schema.think_values || []).length) {
      fields.push({id: schema.think_id, name: "思考强度", values: schema.think_values});
    }
    if (schema.has_fast) fields.push({id: "fast", name: "Fast", kind: "boolean"});
    (schema.params || []).forEach(p => {
      if (p && p.id && !fields.some(f => f.id === p.id)) fields.push(p);
    });
    return fields;
  }
  function normalize(model, config) {
    fieldsOf(model).forEach(field => {
      const values = field.values || [];
      const current = config.values[field.id];
      if (field.kind === "boolean") {
        config.values[field.id] = current === "true" ? "true" : "false";
      } else if (values.length && !values.some(v => String(v.id) === current)) {
        config.values[field.id] = String(values[0].id);
      }
    });
    return config;
  }
  function plan(model, config) {
    if (!model) return null;
    const schema = model.schema || {};
    config = normalize(model, config || configOf(model));
    const parameters = (model.parameters || []).map(p => ({id: p.id, value: String(p.value)}));
    fieldsOf(model).forEach(field => {
      if (config.values[field.id] == null) return;
      const at = parameters.findIndex(p => p.id === field.id);
      const value = {id: field.id, value: config.values[field.id]};
      if (at >= 0) parameters[at] = value; else parameters.push(value);
    });
    const max = schema.supports_max && schema.supports_std ? config.max
      : schema.supports_max && schema.supports_std === false ? true
      : schema.supports_std && !schema.supports_max ? false : !!model.max;
    return {model: model.model, max: max, parameters: parameters,
      effort: config.values[schema.think_id] || "",
      fast: schema.has_fast ? config.values.fast === "true" : null};
  }
  function mount(container, models) {
    container.replaceChildren();
    const label = document.createElement("label");
    label.textContent = "新任务模型";
    label.style.cssText = "display:block;margin:10px 0 5px";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "新任务模型");
    select.style.cssText = "width:100%;padding:9px;background:var(--bg-2,#fff);color:inherit;border:1px solid #51515b;border-radius:8px";
    select.add(new Option("沿用目标窗口当前模型", ""));
    const entries = (models || []).filter(m => m && m.model);
    entries.forEach((m, i) => select.add(new Option(m.label || m.model, String(i))));
    const params = document.createElement("div");
    params.style.cssText = "display:grid;gap:8px;margin:10px 0";
    const selected = () => select.value === "" ? null : entries[Number(select.value)];
    const render = () => {
      params.replaceChildren();
      const model = selected();
      if (!model) return;
      const config = normalize(model, configOf(model));
      const add = (field, max) => {
        const row = document.createElement("label");
        row.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:12px";
        const name = document.createElement("span");
        name.textContent = field.name || field.id;
        row.appendChild(name);
        if (field.kind === "boolean") {
          const input = document.createElement("input");
          input.type = "checkbox";
          input.setAttribute("aria-label", name.textContent);
          input.checked = max ? config.max : config.values[field.id] === "true";
          input.onchange = () => { if (max) config.max = input.checked; else config.values[field.id] = String(input.checked); };
          row.appendChild(input);
        } else if ((field.values || []).length) {
          const input = document.createElement("select");
          input.setAttribute("aria-label", name.textContent);
          input.style.cssText = "max-width:60%;padding:5px;background:var(--bg-2,#fff);color:inherit;border:1px solid #51515b;border-radius:6px";
          field.values.forEach(v => input.add(new Option(v.label || v.id, v.id)));
          input.value = config.values[field.id];
          input.onchange = () => { config.values[field.id] = input.value; };
          row.appendChild(input);
        } else return;
        params.appendChild(row);
      };
      fieldsOf(model).forEach(field => add(field, false));
      const schema = model.schema || {};
      if (schema.supports_max && schema.supports_std) add({id: "max", name: "Max 模式", kind: "boolean"}, true);
    };
    select.onchange = render;
    container.append(label, select, params);
    return {selected: () => plan(selected()), select: select};
  }
  root.RxyyMcpModelPicker = {mount: mount, plan: plan, fieldsOf: fieldsOf};
  root.ChijiuModelPicker = root.RxyyMcpModelPicker; // legacy cached pages
})(typeof window === "undefined" ? globalThis : window);
