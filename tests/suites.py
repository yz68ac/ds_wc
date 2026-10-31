"""Explicit ownership of offline regressions; unclassified tests fail discovery."""
from pathlib import Path

SUITES = {
    'models': (
        'test_gemini_connection',
        'test_gemini_native',
        'test_model_providers',
        'test_native_model_protocols',
        'test_optional_output_limit',
        'test_sdk_model_transport',
    ),
    'wechat': (
        'test_analysis_audit',
        'test_analysis_cli',
        'test_analysis_disassembly',
        'test_analysis_elf',
        'test_analysis_inventory',
        'test_chatroom_aliases',
        'test_crypto',
        'test_image_download',
        'test_login',
        'test_native_image_send',
        'test_native_images',
        'test_sdk_options',
        'test_wechat_library',
        'test_wxgf',
    ),
    'bot': (
        'test_avatars',
        'test_chat_payload',
        'test_citation_output',
        'test_context',
        'test_context_focus',
        'test_context_pipeline',
        'test_conversation',
        'test_dynamic_context',
        'test_group_model_runtime',
        'test_group_profile',
        'test_identity_pipeline',
        'test_image_generation',
        'test_instance_control',
        'test_instances',
        'test_journal',
        'test_learning',
        'test_media',
        'test_memory',
        'test_memory_contracts',
        'test_memory_labels',
        'test_memoryfs',
        'test_native_api',
        'test_native_image_bot',
        'test_output',
        'test_parsing',
        'test_participation_config',
        'test_participation_decisions',
        'test_participation_prompt',
        'test_participation_routing',
        'test_participation_stages',
        'test_participation_storage',
        'test_pats',
        'test_polling',
        'test_private_replies',
        'test_prompt_contracts',
        'test_prompts',
        'test_queue_inspection',
        'test_references',
        'test_relationships',
        'test_review_boundaries',
        'test_review_recovery',
        'test_runtime',
        'test_search',
        'test_store',
        'test_ui',
        'test_worker_lifecycle',
    ),
    'management': (
        'test_application_cli',
        'test_boot_recovery',
        'test_host_management',
        'test_instance_operations',
        'test_monitor',
        'test_native_image_deployment',
        'test_offline_guard',
        'test_recover_focus',
        'test_release',
        'test_review_operations',
        'test_service_supervisor',
        'test_web_console',
        'test_web_models',
    ),
}


def selected_modules(suites=()):
    known = {path.stem for path in Path(__file__).parent.glob('test_*.py')}
    declared = [name for values in SUITES.values() for name in values]
    if len(declared) != len(set(declared)) or set(declared) != known:
        raise ValueError('Offline suite manifest must list every test module exactly once: '
                         + ', '.join(sorted(known.symmetric_difference(declared))))
    selected = tuple(dict.fromkeys(suites)) or tuple(SUITES)
    if set(selected)-SUITES.keys():
        raise ValueError('Unknown offline suite')
    return tuple('tests.'+name for name in sorted(name for key in selected for name in SUITES[key]))
