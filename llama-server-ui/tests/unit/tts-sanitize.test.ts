import { sanitizeTextForTTS } from '$lib/data/piper-voices';
import { describe, expect, it } from 'vitest';

// Etiquetas escapadas: el fuente queda limpio y no depende de copiar/pegar.
const OPEN_THINK = '\u003cthinking\u003e';
const CLOSE_THINK = '\u003c/thinking\u003e';

describe('sanitizeTextForTTS (razonamiento)', () => {
	it('elimina bloques de razonamiento cerrados', () => {
		const result = sanitizeTextForTTS(
			`Hola ${OPEN_THINK}estoy pensando mucho${CLOSE_THINK} mundo.`
		);

		expect(result).not.toContain('pensando');
		expect(result).toContain('Hola');
		expect(result).toContain('mundo');
	});

	it('elimina reasoning, thought y analysis cerrados', () => {
		expect(sanitizeTextForTTS('<reasoning>oculto</reasoning>visible')).not.toContain('oculto');
		expect(sanitizeTextForTTS('<thought>oculto</thought>visible')).not.toContain('oculto');
		expect(sanitizeTextForTTS('<analysis>oculto</analysis>visible')).not.toContain('oculto');
	});

	it('descarta un bloque sin cerrar (stream incompleto)', () => {
		const result = sanitizeTextForTTS(
			'Respuesta final. ' + OPEN_THINK + 'texto a medias sin cerrar'
		);

		expect(result).not.toContain('a medias');
		expect(result).toContain('Respuesta final.');
	});

	it('no elimina contenido legitimo ni texto normal', () => {
		expect(sanitizeTextForTTS('Hola, que tal?')).toBe('Hola, que tal?');
		expect(sanitizeTextForTTS('El pensamiento critico es clave.')).toContain('pensamiento critico');
	});
});
