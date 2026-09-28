<script lang="ts">
	import { BookOpen, Check, File, FolderOpen, MessageSquare, Plus, Zap } from '@lucide/svelte';
	import {
		ChatFormActionAddMcpServersSubmenu,
		ChatFormActionAddReasoningSubmenu,
		ChatFormActionAddToolsSubmenu
	} from '$lib/components/app';
	import { buttonVariants } from '$lib/components/ui/button';
	import * as DropdownMenu from '$lib/components/ui/dropdown-menu';
	import * as Tooltip from '$lib/components/ui/tooltip';
	import { cn } from '$lib/components/ui/utils';
	import {
		ATTACHMENT_FILE_ITEMS,
		ATTACHMENT_TOOLTIP_TEXT,
		ICON_CLASS_DEFAULT,
		TOOLTIP_DELAY_DURATION
	} from '$lib/constants';
	import { getChatFormActionsContext } from '$lib/contexts';
	import { useAttachmentMenu } from '$lib/hooks/use-attachment-menu.svelte';
	import { ragActiveLibrary, ragLibraries, ragResearchMode, ragStore } from '$lib/stores/rag.svelte';
	import { onMount } from 'svelte';

	interface Props {
		class?: string;
	}

	let { class: className = '' }: Props = $props();

	const chatFormActions = getChatFormActionsContext();

	let dropdownOpen = $state(false);
	// The system message action moves focus to the message editor, so the menu
	// must not restore focus to the trigger on close
	let suppressCloseAutoFocus = false;

	onMount(() => {
		void ragStore.fetchLibraries();
		void ragStore.fetchActive();
	});

	function handleMcpSettingsClick() {
		dropdownOpen = false;
		chatFormActions.onMcpSettingsClick?.();
	}

	const attachmentMenu = useAttachmentMenu(
		() => ({
			hasAudioModality: chatFormActions.hasAudioModality,
			hasMcpPromptsSupport: chatFormActions.hasMcpPromptsSupport,
			hasMcpResourcesSupport: chatFormActions.hasMcpResourcesSupport,
			hasVideoModality: chatFormActions.hasVideoModality,
			hasVisionModality: chatFormActions.hasVisionModality
		}),
		() => ({
			onFileUpload: chatFormActions.onFileUpload,
			onMcpPromptClick: chatFormActions.onMcpPromptClick,
			onMcpResourcesClick: chatFormActions.onMcpResourcesClick,
			onSystemPromptClick: chatFormActions.onSystemPromptClick
		}),
		() => {
			dropdownOpen = false;
		}
	);
</script>

<div class="flex items-center gap-1 {className}">
	<DropdownMenu.Root bind:open={dropdownOpen}>
		<!-- ignoreNonKeyboardFocus prevents the tooltip from flashing when the
		     menu closes and focus returns to the trigger -->
		<Tooltip.Root ignoreNonKeyboardFocus>
			<Tooltip.Trigger>
				{#snippet child({ props })}
					<DropdownMenu.Trigger
						{...props}
						class={cn(
							buttonVariants({ variant: 'secondary' }),
							'file-upload-button h-8 w-8 cursor-pointer rounded-full p-0'
						)}
						disabled={chatFormActions.disabled}
					>
						<span class="sr-only">{ATTACHMENT_TOOLTIP_TEXT}</span>

						<Plus class={ICON_CLASS_DEFAULT} />
					</DropdownMenu.Trigger>
				{/snippet}
			</Tooltip.Trigger>

			<Tooltip.Content>
				<p>{ATTACHMENT_TOOLTIP_TEXT}</p>
			</Tooltip.Content>
		</Tooltip.Root>

		<DropdownMenu.Content
			align="start"
			class="w-52"
			onCloseAutoFocus={(e) => {
				if (suppressCloseAutoFocus) {
					suppressCloseAutoFocus = false;
					e.preventDefault();
				}
			}}
		>
			<ChatFormActionAddReasoningSubmenu />

			<DropdownMenu.Separator />

			<DropdownMenu.Sub>
				<DropdownMenu.SubTrigger class="flex cursor-pointer items-center gap-2">
					<File class={ICON_CLASS_DEFAULT} />

					<span>Add files</span>
				</DropdownMenu.SubTrigger>

				<DropdownMenu.SubContent class="w-48">
					{#each ATTACHMENT_FILE_ITEMS as item (item.id)}
						{@const enabled = attachmentMenu.isItemEnabled(item.enabledWhen)}
						{#if enabled}
							<DropdownMenu.Item
								class="{item.class ?? ''} flex cursor-pointer items-center gap-2"
								onclick={() => attachmentMenu.callbacks[item.action]()}
							>
								<item.icon class={ICON_CLASS_DEFAULT} />

								<span>{item.label}</span>
							</DropdownMenu.Item>
						{:else if item.disabledTooltip}
							<Tooltip.Root delayDuration={TOOLTIP_DELAY_DURATION}>
								<Tooltip.Trigger tabindex={-1}>
									{#snippet child({ props })}
										<div {...props} class="cursor-default">
											<DropdownMenu.Item
												class="{item.class ?? ''} flex items-center gap-2"
												disabled
											>
												<item.icon class={ICON_CLASS_DEFAULT} />

												<span>{item.label}</span>
											</DropdownMenu.Item>
										</div>
									{/snippet}
								</Tooltip.Trigger>

								<Tooltip.Content side="right">
									<p>{item.disabledTooltip}</p>
								</Tooltip.Content>
							</Tooltip.Root>
						{/if}
					{/each}
				</DropdownMenu.SubContent>
			</DropdownMenu.Sub>

			<DropdownMenu.Item
				class="flex cursor-pointer items-center gap-2"
				onclick={() => {
					suppressCloseAutoFocus = true;
					chatFormActions.onSystemPromptClick?.();
				}}
			>
				<MessageSquare class={ICON_CLASS_DEFAULT} />

				<span>System Message</span>
			</DropdownMenu.Item>

			<DropdownMenu.Sub>
				<DropdownMenu.SubTrigger class="flex cursor-pointer items-center gap-2">
					<BookOpen class={ICON_CLASS_DEFAULT} />
					<span class="flex-1">Investigación RAG</span>
					<span class="max-w-24 truncate text-xs text-muted-foreground">{$ragActiveLibrary || 'Ninguna'}</span>
				</DropdownMenu.SubTrigger>
				<DropdownMenu.SubContent class="w-56">
					<DropdownMenu.Item
						class="flex cursor-pointer items-center gap-2"
						onclick={() => ragStore.toggleResearchMode()}
						aria-pressed={$ragResearchMode}
						title="Activa respuestas académicas basadas exclusivamente en la biblioteca seleccionada."
					>
						{#if $ragResearchMode}<Check class={ICON_CLASS_DEFAULT} />{:else}<BookOpen class={ICON_CLASS_DEFAULT} />{/if}
						<span class="flex-1">{$ragResearchMode ? 'Desactivar investigación' : 'Activar investigación'}</span>
					</DropdownMenu.Item>
					<DropdownMenu.Separator />
					<span class="text-muted-foreground px-2 py-1 text-[11px]">Biblioteca activa</span>
					{#if $ragLibraries.length === 0}
						<DropdownMenu.Item disabled>Sin bibliotecas disponibles</DropdownMenu.Item>
					{:else}
						{#each $ragLibraries as library (library)}
							<DropdownMenu.Item
								class="flex cursor-pointer items-center gap-2"
								onclick={() => void ragStore.setActive(library)}
							>
								{#if $ragActiveLibrary === library}<Check class={ICON_CLASS_DEFAULT} />{:else}<span class={ICON_CLASS_DEFAULT}></span>{/if}
								<span class="truncate">{library}</span>
							</DropdownMenu.Item>
						{/each}
					{/if}
				</DropdownMenu.SubContent>
			</DropdownMenu.Sub>

			<ChatFormActionAddToolsSubmenu />

			<ChatFormActionAddMcpServersSubmenu onMcpSettingsClick={handleMcpSettingsClick} />

			{#if chatFormActions.hasMcpPromptsSupport}
				<DropdownMenu.Separator />

				<DropdownMenu.Item
					class="flex cursor-pointer items-center gap-2"
					onclick={chatFormActions.onMcpPromptClick}
				>
					<Zap class={ICON_CLASS_DEFAULT} />

					<span>MCP Prompt</span>
				</DropdownMenu.Item>
			{/if}

			{#if chatFormActions.hasMcpResourcesSupport}
				<DropdownMenu.Item
					class="flex cursor-pointer items-center gap-2"
					onclick={chatFormActions.onMcpResourcesClick}
				>
					<FolderOpen class={ICON_CLASS_DEFAULT} />

					<span>MCP Resources</span>
				</DropdownMenu.Item>
			{/if}
		</DropdownMenu.Content>
	</DropdownMenu.Root>
</div>
