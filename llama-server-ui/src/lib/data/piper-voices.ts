/**
 * Piper TTS Voice Metadata
 *
 * Metadatos para las voces de Piper disponibles en `app_data/piper/whisper/models/voices`.
 *
 * Piper usa un modelo por voz (cada `.onnx` + `.json`), y el idioma viene
 * determinado por la propia voz (el prefijo es_MX- / en_us-). No hace falta
 * enviar `lang` al sintetizar: basta con el id de la voz.
 */
export interface VoiceMeta {
	/** Id de la voz = nombre del archivo .onnx (sin extensión) */
	id?: string;
	/** Nombre para mostrar */
	name: string;
	/** Género: 'f' = femenina, 'm' = masculina */
	gender: 'f' | 'm';
	/** Calidad global (A- a F) */
	grade?: string;
	/** Idioma base para agrupar / selector de idioma (no afecta al TTS) */
	piperLang: string;
	/** Etiqueta de idioma legible */
	langLabel: string;
	/** Emoji de bandera */
	flag: string;
	/** Rasgos especiales */
	traits?: string;
}

export interface LanguageGroup {
	code: string;
	label: string;
	flag: string;
	voices: VoiceMeta[];
}

export const VOICE_GRADES: Record<string, { label: string; color: string }> = {
	'A-': { label: 'Excelente', color: '#22c55e' },
	'A': { label: 'Excelente', color: '#22c55e' },
	'B+': { label: 'Muy buena', color: '#84cc16' },
	'B': { label: 'Buena', color: '#84cc16' },
	'B-': { label: 'Buena', color: '#a3e635' },
	'C+': { label: 'Aceptable', color: '#eab308' },
	'C': { label: 'Aceptable', color: '#f59e0b' },
	'C-': { label: 'Básica', color: '#f97316' },
	'D+': { label: 'Básica', color: '#ef4444' },
	'D': { label: 'Limitada', color: '#ef4444' },
	'D-': { label: 'Limitada', color: '#dc2626' },
	'F+': { label: 'Mínima', color: '#991b1b' },
	'F': { label: 'Mínima', color: '#991b1b' }
};

/**
 * Metadatos de las voces Piper indexados por id de voz.
 * Solo se muestran las voces que el servidor reporte en /voices.
 */
export const VOICE_METADATA: Record<string, VoiceMeta> = {
	// ── Español (México) ─────────────────────────────────────────────────────
	'es_MX-claude-14947-epoch-high': { name: 'Claude', gender: 'f', grade: 'A', piperLang: 'es', langLabel: 'Español (MX)', flag: '🇲🇽', traits: '⚡' },
	'es_MX-cortana-19669-epoch-high': { name: 'Cortana', gender: 'f', grade: 'A-', piperLang: 'es', langLabel: 'Español (MX)', flag: '🇲🇽' },
	'es_MX-emma-high': { name: 'Emma', gender: 'f', grade: 'B+', piperLang: 'es', langLabel: 'Español (MX)', flag: '🇲🇽' },
	'es_MX-laura-high': { name: 'Laura', gender: 'f', grade: 'B', piperLang: 'es', langLabel: 'Español (MX)', flag: '🇲🇽' },

	// ── English (US) ─────────────────────────────────────────────────────────
	'en_us-jarvis_ucm-high': { name: 'Jarvis', gender: 'm', grade: 'A', piperLang: 'en', langLabel: 'English (US)', flag: '🇺🇸', traits: '🤖' }
};

/**
 * Voz por defecto de Piper (debe existir en VOICE_METADATA y en el disco).
 */
export const DEFAULT_PIPER_VOICE = 'es_MX-claude-14947-epoch-high';

/**
 * Información de grupo por etiqueta de idioma (para la agrupación en la UI).
 */
const LANG_GROUPS: Record<string, { code: string; label: string; flag: string }> = {
	'Español (MX)': { code: 'es', label: 'Español (MX)', flag: '🇲🇽' },
	'English (US)': { code: 'en', label: 'English (US)', flag: '🇺🇸' }
};

const GROUP_ORDER = ['Español (MX)', 'English (US)'];

const GRADE_ORDER = ['A-', 'A', 'B+', 'B', 'B-', 'C+', 'C', 'C-', 'D+', 'D', 'D-', 'F+', 'F'];

/**
 * Voz agrupadas por idioma, ordenadas por calidad (mejor primero).
 * Solo incluye voces presentes en `availableVoices` (las que reporta /voices).
 */
export function getGroupedVoices(availableVoices: string[]): LanguageGroup[] {
	const groups: Map<string, LanguageGroup> = new Map();

	const gradeIndex = (g: string) => {
		const idx = GRADE_ORDER.indexOf(g);
		return idx >= 0 ? idx : GRADE_ORDER.length;
	};

	for (const voiceName of availableVoices) {
		const meta = VOICE_METADATA[voiceName];
		if (!meta) continue;

		const groupKey = meta.langLabel;
		const groupInfo = LANG_GROUPS[groupKey];

		if (!groups.has(groupKey)) {
			groups.set(groupKey, {
				code: groupInfo?.code ?? groupKey,
				label: groupInfo?.label ?? groupKey,
				flag: groupInfo?.flag ?? '🌐',
				voices: []
			});
		}

		groups.get(groupKey)!.voices.push({
			...meta,
			id: voiceName,
			name: meta.name ?? voiceName
		});
	}

	for (const group of groups.values()) {
		group.voices.sort((a, b) => {
			const ga = gradeIndex(a.grade ?? '');
			const gb = gradeIndex(b.grade ?? '');
			if (ga !== gb) return ga - gb;
			return a.name.localeCompare(b.name);
		});
	}

	return Array.from(groups.values()).sort((a, b) => {
		const ai = GROUP_ORDER.indexOf(a.label);
		const bi = GROUP_ORDER.indexOf(b.label);
		return (ai >= 0 ? ai : 99) - (bi >= 0 ? bi : 99);
	});
}

/**
 * Devuelve el idioma base para una voz Piper (informativo; el TTS lo decide la propia voz).
 */
export function getVoiceLang(voiceName: string): string {
	const meta = VOICE_METADATA[voiceName];
	if (!meta) return 'es';
	return meta.piperLang ?? 'es';
}
// Text sanitization for TTS
// ─────────────────────────────────────────────────────────────────────────────

/** Regex to match emoji characters (full range including composed) */
const EMOJI_REGEX =
	/[\u{1F600}-\u{1F64F}\u{1F300}-\u{1F5FF}\u{1F680}-\u{1F6FF}\u{1F1E0}-\u{1F1FF}\u{1F900}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}\u{FE00}-\u{FE0F}\u{1F000}-\u{1F02F}\u{1F0A0}-\u{1F0FF}\u{1F100}-\u{1F1FF}\u{1FA00}-\u{1FA6F}\u{1FA70}-\u{1FAFF}\u{200D}\u{20E3}\u{E0020}-\u{E007F}]/gu;

/** Markdown bold/italic/strikethrough markers */
const MD_BOLD_ITALIC = /\*{1,3}|_{1,3}|~~(?!~)/g;

/** Markdown heading markers */
const MD_HEADING = /^#{1,6}\s+/gm;

/** Markdown code blocks and inline code */
const MD_CODE_BLOCK = /```[\s\S]*?```/g;
const MD_INLINE_CODE = /`[^`]+`/g;

/** Markdown links: [text](url) → text */
const MD_LINKS = /\[([^\]]+)\]\([^)]+\)/g;

/** Markdown images: ![alt](url) → (empty) */
const MD_IMAGES = /!\[([^\]]*)\]\([^)]+\)/g;

/** Markdown list markers */
const MD_LIST = /^\s*[-*+]\s+/gm;

/** Markdown horizontal rules */
const MD_HR = /^[-*_]{3,}\s*$/gm;

/** Markdown blockquotes */
const MD_QUOTE = /^>\s*/gm;

/** Bloques de razonamiento/thinking: NUNCA deben leerse en voz alta */
const REASONING_BLOCK =
	/<(think|thinking|reasoning|thought|analysis|scratchpad)>[\s\S]*?<\/(?:think|thinking|reasoning|thought|analysis|scratchpad)>/gi;

/** Apertura de razonamiento sin cierre (stream incompleto): descartar el resto */
const REASONING_OPEN = /<(?:think|thinking|reasoning|thought|analysis|scratchpad)>[\s\S]*$/gi;

/** Excessive whitespace */
const EXCESSIVE_WS = /[ \t]{2,}/g;

/** Multiple newlines (keep sentence breaks but remove excessive) */
const MULTI_NL = /\n{3,}/g;

/**
 * Sanitize text for TTS synthesis.
 * Removes emojis, markdown formatting, and non-speakable characters
 * while preserving the natural text content.
 */
export function sanitizeTextForTTS(text: string): string {
	if (!text) return '';

	let cleaned = text;

	// Reasoning/thinking fuera antes que nada: es texto interno del modelo y no
	// debe oírse (el modo manual pide explícitamente "sin razonamiento").
	cleaned = cleaned.replace(REASONING_BLOCK, ' ');
	cleaned = cleaned.replace(REASONING_OPEN, ' ');

	// Remove markdown images first (before links, to avoid partial matches)
	cleaned = cleaned.replace(MD_IMAGES, '');

	// Replace markdown links with just the text
	cleaned = cleaned.replace(MD_LINKS, '$1');

	// Remove code blocks entirely (they don't sound good spoken)
	cleaned = cleaned.replace(MD_CODE_BLOCK, '');

	// Remove inline code backticks but keep content
	cleaned = cleaned.replace(MD_INLINE_CODE, (match) => match.slice(1, -1));

	// Remove markdown heading markers
	cleaned = cleaned.replace(MD_HEADING, '');

	// Remove bold/italic/strikethrough markers
	cleaned = cleaned.replace(MD_BOLD_ITALIC, '');

	// Remove list markers
	cleaned = cleaned.replace(MD_LIST, '');

	// Remove horizontal rules
	cleaned = cleaned.replace(MD_HR, '');

	// Remove blockquote markers
	cleaned = cleaned.replace(MD_QUOTE, '');

	// Remove residual Markdown punctuation that can survive nested formatting,
	// tables, task boxes, or emphasis spans.
	cleaned = cleaned.replace(/^[ \t]*[-*_]{3,}[ \t]*$/gm, '');
	cleaned = cleaned.replace(/[\*_~#]+/g, '');
	cleaned = cleaned.replace(/^\s*(?:\[[ xX]\]|\|)\s*/gm, '');

	// Remove all emojis
	cleaned = cleaned.replace(EMOJI_REGEX, '');

	// Remove common symbols that sound bad when spoken
	cleaned = cleaned.replace(/[§¶†‡※◆◇○●□■△▲▽▼♦♣♠♥★☆⚡✦🔀🔁⏭️▶️⏹️⏸️]/g, '');

	// Remove URL patterns (they don't sound good spoken)
	cleaned = cleaned.replace(/https?:\/\/\S+/g, 'enlace');

	// Collapse excessive whitespace
	cleaned = cleaned.replace(EXCESSIVE_WS, ' ');

	// Collapse multiple newlines to double (sentence break)
	cleaned = cleaned.replace(MULTI_NL, '\n\n');

	// Trim
	cleaned = cleaned.trim();

	return cleaned;
}
