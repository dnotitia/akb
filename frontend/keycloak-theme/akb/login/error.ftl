<#-- Error page. Replaces base/login/error.ftl: same content, AKB layout. -->
<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        ${kcSanitize(msg("errorTitle"))?no_esc}
    <#elseif section = "form">
        <div id="kc-error-message" class="akb-message">
            <div class="akb-alert akb-alert--error" role="alert">
                <@layout.icon name="error"/>
                <p class="akb-alert__text instruction">${kcSanitize(message.summary)?no_esc}</p>
            </div>
            <#if traceId??>
                <p class="instruction akb-message__trace" id="traceId">${msg("traceIdSupportMessage", traceId)}</p>
            </#if>
            <#if skipLink??>
            <#else>
                <#if client?? && client.baseUrl?has_content>
                    <p class="akb-form-actions"><a id="backToApplication" class="akb-button akb-button--outline akb-button--block akb-button--lg" href="${client.baseUrl}">${msg("backToApplication")}</a></p>
                </#if>
            </#if>
        </div>
    </#if>
</@layout.registrationLayout>
