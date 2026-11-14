// Audit Receipts tab: chained HMAC receipts, per-receipt verify, tamper demo, and full-chain verify.
import { api } from './api.js';
import { badge, clear, el, facts, section, sourceNotice, stateBadge, table, text, utcTime, verdictBadge } from './dom.js';

let chain = null;

function resultView(result, prefix) {
  return el('div', { class: 'stage-row' }, [
    verdictBadge(result.valid),
    el('b', { class: 'small', text: `${prefix}${result.reason}` }),
    el('b', { class: 'small muted', text: `${text(result.latency_ms)} ms server` }),
  ]);
}

async function verifyOne(item, output, tamper) {
  const receipt = structuredClone(item.receipt);
  let prefix = '';
  if (tamper) {
    const flipped = receipt.decision === 'PASS' ? 'BLOCK' : 'PASS';
    prefix = `Tampered decision ${receipt.decision} -> ${flipped}: `;
    receipt.decision = flipped;
  }
  clear(output).append(el('b', { class: 'small muted', text: tamper ? 'Verifying tampered copy' : 'Verifying' }));
  try {
    const { data } = await api('/api/receipts/verify', { method: 'POST', body: { receipt, signature: item.signature } });
    clear(output).append(resultView(data, prefix));
  } catch (err) {
    clear(output).append(el('b', { class: 'small', role: 'alert', text: `Verify failed: ${err.message}` }));
  }
}

function highlight(rows, seq) {
  for (const row of rows) {
    row.classList.toggle('is-highlight', row.dataset.seq === String(seq));
  }
}

function receiptsTable(data) {
  const outputs = new Map();
  const rows = data.receipts.map((item) => {
    const body = item.receipt;
    const output = el('output', { 'data-result': body.id, 'aria-live': 'polite' }, [el('b', { class: 'small muted', text: 'Not verified yet' })]);
    outputs.set(body.id, output);
    const prevCell = body.seq > 1
      ? el('button', { class: 'btn btn-small', type: 'button', 'data-prev': body.seq - 1, title: 'Highlight the previous receipt' },
        [el('code', { text: item.short_prev_signature })])
      : el('code', { text: item.short_prev_signature });
    return {
      attrs: { 'data-seq': body.seq, 'data-receipt': body.id },
      cells: {
        id: el('div', {}, [el('code', { text: body.id }), el('p', { class: 'small muted', text: `#${body.seq}` })]),
        stage: el('code', { text: body.stage }),
        decision: el('div', { class: 'stage-row' }, [stateBadge(body.decision), el('b', { class: 'small', text: body.gate })]),
        time: utcTime(body.timestamp),
        signature: el('code', { title: item.signature, text: item.short_signature }),
        prev: prevCell,
        actions: el('div', { class: 'btn-row' }, [
          el('button', { class: 'btn btn-small', type: 'button', 'data-verify': body.id, onclick: () => verifyOne(item, output, false) }, ['Verify']),
          el('button', { class: 'btn btn-small', type: 'button', 'data-tamper': body.id, onclick: () => verifyOne(item, output, true) }, ['Tamper & verify']),
        ]),
        result: output,
      },
    };
  });
  const columns = [
    { key: 'id', label: 'Receipt' }, { key: 'stage', label: 'Stage' }, { key: 'decision', label: 'Decision' },
    { key: 'time', label: 'Timestamp' }, { key: 'signature', label: 'Signature' }, { key: 'prev', label: 'Prev link' },
    { key: 'actions', label: 'Actions' }, { key: 'result', label: 'Result' },
  ];
  const node = table(columns, rows, `${data.count} receipts in chain ${data.chain_id}`);
  const trs = Array.from(node.querySelectorAll('tbody tr'));
  node.addEventListener('click', (event) => {
    const button = event.target.closest('[data-prev]');
    if (button) highlight(trs, button.dataset.prev);
  });
  return node;
}

export async function loadReceipts(container, chainButton) {
  const { data } = await api('/api/receipts');
  chain = data;
  const chainResult = el('output', { id: 'chain-result', 'aria-live': 'polite' }, [stateBadge(data.chain_valid ? 'PASS' : 'FAIL'),
    el('b', { class: 'small muted', text: ' chain status reported by the server' })]);
  clear(container).append(
    sourceNotice(data.source_label, 'receipts'),
    section('Signing key and format', 'key', [facts([
      ['Algorithm', badge(data.algorithm, 'info')],
      ['Key id (fingerprint)', el('code', { id: 'receipts-key-id', text: data.key_id })],
      ['Key source', el('code', { text: `env ${data.key_env_var}` })],
      ['Canonicalization', data.canonicalization],
      ['Genesis link', el('code', { text: data.genesis_prev_signature })],
      ['Full chain', chainResult],
    ])]),
    section('Chained receipts', 'receipt', [receiptsTable(data)]),
  );
  chainButton.onclick = async () => {
    clear(chainResult).append(el('b', { class: 'small muted', text: 'Verifying full chain' }));
    try {
      const items = chain.receipts.map((item) => ({ receipt: item.receipt, signature: item.signature }));
      const { data: result } = await api('/api/receipts/verify', { method: 'POST', body: { chain: items } });
      clear(chainResult).append(resultView(result, 'Full chain: '));
    } catch (err) {
      clear(chainResult).append(el('b', { class: 'small', role: 'alert', text: `Chain verify failed: ${err.message}` }));
    }
  };
}
