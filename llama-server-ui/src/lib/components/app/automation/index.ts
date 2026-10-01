/**
 * Panel de automatización 24/7.
 *
 * Consulta y configura el servicio 24/7 (`tools/automation_service.py`, puerto
 * 15001): cola persistente, dispatchers, aprobaciones y auditoría. La UI no
 * ejecuta nada por sí misma. El servicio corre en su propio proceso —cerrar la
 * web view no lo detiene— pero lo arranca y lo para el Manager, porque sin
 * manager no hay modelo y los dispatchers no tendrían nada que ejecutar.
 */
export { default as AutomationPanel } from './AutomationPanel.svelte';
