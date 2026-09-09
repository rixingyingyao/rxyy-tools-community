"""Native question cards retain drafts and never imply an uncertain reply succeeded."""
import unittest
from pathlib import Path

from test_timeline_display import FAKE_DOM, _node

ROOT = Path(__file__).resolve().parents[1]
JS = FAKE_DOM + r'''
El.prototype.replaceChildren = function(...xs) { this.children = xs; };
El.prototype.setAttribute = function(k, v) { this[k] = v; };
El.prototype.focus = function() {};
document.head = new El("head");
globalThis.window = {};
''' + (ROOT / "native_questions.js").read_text(encoding="utf-8") + r'''
const eq = (a,b) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(JSON.stringify([a,b])); };
const container = new El("div");
const turn = {turn_id:"turn", desktop_connected:true, native_questions:[{
  item_id:"call", delivery:"async", status:"pending", answers:{},
  questions:[{id:"q1",question:"选择格式",options:["JSON","Markdown"]}]}]};
const config = {sid:"session",threadId:"thread",refresh:()=>{},submit:async()=>({ok:true})};
const render = () => window.RxyyMcpNativeQuestions.render(container,turn,config);
'''


class NativeQuestionsTests(unittest.TestCase):
    def run_js(self, js):
        code, output = _node(JS + '(async () => {\n' + js + '\n})().catch(e => {console.error(e);process.exitCode=1;});')
        self.assertEqual(0, code, output)

    def test_draft_survives_refresh_and_double_click_sends_only_once(self):
        self.run_js(r'''
render();
container.find("nq-option")[0].onclick();
turn.native_questions[0].requested_at=1; render();
eq(container.find("nq-input")[0].value,"JSON");
const calls=[]; let finish;
config.submit=async p=>{calls.push(p);return new Promise(r=>finish=r);};
const button=container.find("nq-submit")[0];
const sent=button.onclick(); await button.onclick();
eq(calls.length,1); eq(calls[0],{sid:"session",thread_id:"thread",turn_id:"turn",request_id:"call",answers:{q1:"JSON"}});
finish({ok:true,delivery:"native_accepted"}); await sent;
turn.native_questions[0].requested_at=2; render();
eq(container.find("nq-submit")[0].disabled,true);
''')

    def test_unknown_delivery_stays_disabled_after_refresh(self):
        self.run_js(r'''
config.submit=async()=>{throw new Error("connection lost");};
render(); container.find("nq-option")[0].onclick(); await container.find("nq-submit")[0].onclick();
turn.native_questions[0].requested_at=2; render();
eq(container.find("nq-submit")[0].disabled,true);
if(!container.find("nq-note")[0].textContent.includes("结果未确认")) throw new Error("unknown must not be marked answered");
''')

    def test_partial_native_answer_keeps_remaining_question_editable(self):
        self.run_js(r'''
turn.native_questions[0].questions.push({id:"q2",question:"文件名",options:[]});
turn.native_questions[0].answers={q1:"JSON"};
const calls=[];config.submit=async p=>{calls.push(p);return {ok:true};};
render();eq(container.find("nq-input").length,1);
await container.find("nq-submit")[0].onclick();eq(calls.length,0);
const input=container.find("nq-input")[0]; input.value="result.json";input.oninput();
await container.find("nq-submit")[0].onclick();eq(calls[0].answers,{q2:"result.json"});
turn.native_questions[0].status="resolved";render();
eq(container.find("nq-state")[0].textContent,"已回答");
''')


if __name__ == "__main__":
    unittest.main()
