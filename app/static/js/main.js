document.addEventListener('DOMContentLoaded', () => {
    const themeToggle = document.getElementById('theme-toggle');
    themeToggle.addEventListener('click', () => {
        document.body.classList.toggle('dark-theme');
        document.body.classList.toggle('light-theme');
    });

    fetch('/api/health').then(r => r.json()).then(data => {
        document.getElementById('health-data').innerText = `SLA: ${data.sla}% | SLO: ${data.slo}% | Status: ${data.status}`;
    });

    fetch('/api/rollouts').then(r => r.json()).then(data => {
        document.getElementById('rollouts-data').innerHTML = data.map(d => `<div>${d.id} - ${d.type} (${d.status}, traffic: ${d.traffic}%)</div>`).join('');
    });

    fetch('/api/workflow').then(r => r.json()).then(data => {
        document.getElementById('workflow-data').innerHTML = data.map(d => `<div>${d.stage}: ${d.status}</div>`).join('');
    });

    fetch('/api/dreams').then(r => r.json()).then(data => {
        document.getElementById('dreams-data').innerHTML = `<div>${data.id}: ${data.description}<br>Scorecard: ${data.scorecard}<br>Status: ${data.status}</div>`;
    });

    fetch('/api/receipts').then(r => r.json()).then(data => {
        document.getElementById('receipts-data').innerHTML = data.map(d => `<div>${d.id} - ${d.signature} (${d.timestamp})</div>`).join('');
    });
});
