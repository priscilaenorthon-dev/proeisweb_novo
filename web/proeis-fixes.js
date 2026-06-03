/* Correcoes carregadas apos app.js para manter o patch pequeno na branch principal. */
(function () {
  function cleanVagaLabel(label) {
    return String(label || '')
      .replace(/\s+(?:disponivel\s+)?reserva\s*[-–]\s*curso.*$/i, '')
      .replace(/\s+\d+\s*[-–]\s*curso.*$/i, '')
      .replace(/\s+eu\s+vou\s*$/i, '')
      .trim();
  }

  window.parseVagaLabel = function parseVagaLabel(label) {
    if (!label) return { nome: '', endereco: '' };
    const clean = cleanVagaLabel(label);
    const m = clean.match(/^(.+?)\s+(\d{1,2}:\d{2}:\d{2})\s+(\d+\s*[–\-]\s*[Cc]urso\s+)?(\d+\s*h\s+)?(.*)$/);
    if (m) {
      return {
        nome: m[1].replace(/\s*[-–]\s*$/, '').trim(),
        endereco: (m[5] || '').trim(),
      };
    }
    const m2 = clean.match(/^(.+?)\s+\d{1,2}h\d{2}\s+(.*)$/i);
    if (m2) return { nome: m2[1].trim(), endereco: m2[2].trim() };
    return { nome: clean, endereco: '' };
  };

  window.vagaParaEvento = function vagaParaEvento(idx) {
    const vaga = _vagasStore[idx];
    if (!vaga) return;

    const horaFormatted = vaga.hora
      ? vaga.hora.replace(/^(\d{1,2})h(\d{2})$/i, (_, h, m) => `${h.padStart(2, '0')}:${m}`)
      : '';

    const parsed = window.parseVagaLabel(vaga.label || '');
    const nomeEvento = vaga.nome || parsed.nome;
    const endEvento = vaga.endereco || parsed.endereco;

    const prefill = {
      convenio: vaga.convenio || '',
      cpa: vaga.cpa || '',
      data_evento: vaga.data || '',
      hora_evento: horaFormatted,
      disponivel: vaga.tipo === 'reserva' ? 'reserva' : 'nao-reserva',
      quantidade: 1,
      nome_evento: nomeEvento,
      turno: '',
      endereco: endEvento,
      scan_rounds: 1,
      _label: vaga.label,
    };

    openEventModal(null, prefill, () => {
      showToast('✅ Evento criado! Vagas continuam visíveis.');
    });
  };
})();
