"""Transfer selections must retain each model's own supported parameters."""
import json
import subprocess
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
TRANSFER = (MODULE / "transfer_dialog.js").read_text(encoding="utf-8")

DIALOG_DOM = r'''
const nodes = [];
class Element {
  constructor(tag) { this.tag = tag; this.style = {}; this.attrs = {};
    this.children = []; this.options = []; this.events = {}; this.value = "";
    this.textContent = ""; this.disabled = false; nodes.push(this); }
  appendChild(child) { this.children.push(child); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(key, callback) { this.events[key] = callback; }
  add(option) { this.options.push(option); if (this.options.length === 1) this.value = option.value; }
  replaceChildren() { this.children = []; this.options = []; this.value = ""; }
  focus() {}
  remove() {}
}
global.document = {body: new Element("body"), createElement: tag => new Element(tag)};
global.Option = class { constructor(text, value) { this.text = text; this.value = value; } };
const flush = () => new Promise(resolve => setImmediate(resolve));
const catalog = {ok:true, default_model:"gpt-6-astra", default_effort:"max", models:[
  {model:"gpt-6-astra", label:"Astra", default_effort:"medium", efforts:["medium","max"]},
  {model:"gpt-5.3-codex-spark", label:"Spark", default_effort:"high", efforts:["high","xhigh"]}
]};
function fields() {
  const field = name => nodes.findLast(node => node.attrs["aria-label"] === name);
  return {project:field("项目目录"), prompt:field("任务内容（可留空）"),
    model:field("Codex 模型"), effort:field("Codex 思考程度"),
    go:nodes.findLast(node => node.tag === "button"),
    status:nodes.findLast(node => node.attrs.role === "status")};
}
'''


class TransferModelTests(unittest.TestCase):
    def node(self, script):
        proc = subprocess.run(["node", "-e", (MODULE / "model_picker.js").read_text(
            encoding="utf-8") + script], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(0, proc.returncode, proc.stderr[-2500:])
        return json.loads(proc.stdout)

    def test_supported_selection_survives_and_unrelated_model_is_independent(self):
        result = self.node('''
const a = {model:"a", max:false, parameters:[{id:"effort",value:"high"}],
  schema:{think_id:"effort",think_values:[{id:"low"},{id:"high"}],has_fast:true,
          supports_max:true,supports_std:true}};
const b = {model:"b", max:true, parameters:[{id:"reasoning",value:"medium"}],
  schema:{think_id:"reasoning",think_values:[{id:"medium"},{id:"max"}],has_fast:false,
          supports_max:true,supports_std:false}};
console.log(JSON.stringify([RxyyMcpModelPicker.plan(a,{max:true,values:{effort:"low",fast:"true"}}),
  RxyyMcpModelPicker.plan(b), RxyyMcpModelPicker.plan(a)]));
''')
        self.assertEqual("a", result[0]["model"])
        self.assertEqual("low", result[0]["effort"])
        self.assertTrue(result[0]["max"])
        self.assertTrue(result[0]["fast"])
        self.assertEqual("medium", result[1]["effort"])
        self.assertIsNone(result[1]["fast"])
        self.assertEqual("high", result[2]["effort"])

    def test_extra_parameters_preserved_with_catalog_constraints(self):
        result = self.node('''
const model={model:"a",parameters:[{id:"context",value:"large"}],schema:{params:[
  {id:"context",name:"上下文",values:[{id:"small"},{id:"large"}]},
  {id:"thinking",kind:"boolean"}]}};
console.log(JSON.stringify(RxyyMcpModelPicker.plan(model,
  {max:false,values:{context:"invalid",thinking:"true"}})));
''')
        self.assertEqual([{"id": "context", "value": "small"},
                          {"id": "thinking", "value": "true"}], result["parameters"])

    def test_follow_window_selection_is_null(self):
        self.assertIsNone(self.node("console.log(JSON.stringify(RxyyMcpModelPicker.plan(null)));"))


class NativeNewTaskDialogTests(unittest.TestCase):
    def run_dialog(self, scenario):
        script = DIALOG_DOM + TRANSFER + "\n(async () => {\n" + scenario + (
            "\n})().catch(error => { console.error(error); process.exitCode = 1; });")
        proc = subprocess.run(["node", "-"], input=script, capture_output=True,
                              text=True, encoding="utf-8", timeout=20)
        self.assertEqual(0, proc.returncode, proc.stderr[-2500:])
        return json.loads(proc.stdout)

    def test_effective_defaults_and_explicit_choices_survive_project_change(self):
        result = self.run_dialog(r'''
let paths = [], sent, selected;
RxyyMcpTransferDialog.openNew({project:"D:/one", models:async path => { paths.push(path); return catalog; },
  create:async (...args) => { sent=args; return {ok:true,created:true,dispatched:true,session_id:"native"}; },
  onCreated:async result => { selected=result.session_id; }});
await flush(); const f=fields(); const defaults=[f.model.value,f.effort.value];
f.model.value="gpt-5.3-codex-spark"; f.model.onchange();
f.effort.value="xhigh"; f.effort.onchange();
f.project.value="D:/two"; await f.project.events.change();
f.prompt.value="same prompt"; f.prompt.events.input(); await f.go.onclick();
console.log(JSON.stringify({defaults,paths,sent,selected,locked:f.go.disabled}));
''')
        self.assertEqual(["gpt-6-astra", "max"], result["defaults"])
        self.assertEqual(["D:/one", "D:/two"], result["paths"])
        self.assertEqual(["D:/two", "same prompt", "gpt-5.3-codex-spark", "xhigh"], result["sent"])
        self.assertEqual("native", result["selected"])
        self.assertTrue(result["locked"])

    def test_unknown_result_cannot_be_retried_or_unlocked_by_project_change(self):
        result = self.run_dialog(r'''
const results=[];
for (const response of ["network",null,{}, {ok:false,created:true,error:"setup unknown"}]) {
  let calls=0, reads=0;
  RxyyMcpTransferDialog.openNew({project:"D:/one", models:async () => { reads++; return catalog; },
    create:async () => { calls++; if(response==="network") throw new Error("timeout"); return response; }});
  await flush(); const f=fields(); await f.go.onclick();
  f.project.value="D:/two"; await f.project.events.change(); await f.go.onclick();
  results.push({calls,reads,locked:f.go.disabled,fieldsLocked:f.project.disabled,
    note:f.status.textContent});
}
console.log(JSON.stringify(results));
''')
        for case in result:
            self.assertEqual(1, case["calls"])
            self.assertEqual(1, case["reads"])
            self.assertTrue(case["locked"])
            self.assertTrue(case["fieldsLocked"])
        self.assertIn("先核对任务列表", result[0]["note"])

    def test_explicit_validation_failure_allows_corrected_retry(self):
        result = self.run_dialog(r'''
let calls=0;
RxyyMcpTransferDialog.openNew({project:"D:/one", models:async () => catalog,
  create:async () => ++calls===1 ? {ok:false,error:"请选择可用项目"} : {ok:true,created:true,dispatched:false}});
await flush(); const f=fields(); await f.go.onclick();
const retryEnabled=!f.go.disabled && !f.project.disabled;
f.project.value="D:/corrected"; await f.project.events.change(); await f.go.onclick();
console.log(JSON.stringify({calls,retryEnabled,locked:f.go.disabled,label:f.go.textContent}));
''')
        self.assertTrue(result["retryEnabled"])
        self.assertEqual(2, result["calls"])
        self.assertTrue(result["locked"])
        self.assertEqual("任务已创建", result["label"])


if __name__ == "__main__":
    unittest.main()
