/**
 * Panel de automatización 24/7.
 *
 * Consulta y configura el **servicio independiente** (`tools/automation_service.py`,
 * puerto 15001): cola persistente, dispatchers, aprobaciones y auditoría. La UI
 * no ejecuta nada por sí misma.
 */
export { default as AutomationPanel } from './AutomationPanel.svelte';
