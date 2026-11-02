const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('only one request is sent while a submission is pending', async () => {
  let submit;
  const requests = [];
  const form = {
    dataset: {url: '/api/ask/', machine: 'M-TEST'},
    addEventListener: (_, handler) => { submit = handler; },
  };
  const log = {appendChild() {}, lastElementChild: {scrollIntoView() {}}};
  const input = {value: 'Question', disabled: false, focus() {}};
  const button = {disabled: false, textContent: ''};
  const elements = {'ask-form': form, 'chat-log': log, question: input, 'ask-btn': button};
  const context = {
    document: {
      getElementById: id => elements[id],
      createElement: () => ({remove() {}, className: '', innerHTML: ''}),
    },
    apiFetch: () => new Promise(resolve => requests.push(resolve)),
    escapeHtml: String,
    flattenErrors: String,
  };
  const source = path.join(__dirname, '..', '..', 'static', 'assistant', 'js', 'chat.js');
  vm.runInNewContext(fs.readFileSync(source, 'utf8'), context);

  const event = {preventDefault() {}};
  const first = submit(event);
  input.value = 'Question';
  await submit(event);
  assert.equal(requests.length, 1);
  assert.equal(button.disabled, true);

  requests[0]({ok: true, json: async () => ({
    status: 'insufficient_evidence', machine: 'M-TEST', created_at: new Date().toISOString(),
    answer: 'No evidence', sources: [],
  })});
  await first;
  assert.equal(button.disabled, false);

  const next = submit(event);
  assert.equal(requests.length, 2);
  requests[1]({ok: false, json: async () => ({detail: 'Unavailable'})});
  await next;
  assert.equal(button.disabled, false);
});
