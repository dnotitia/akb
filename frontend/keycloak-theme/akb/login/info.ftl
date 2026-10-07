<#-- Information page. Replaces base/login/info.ftl: same content, AKB layout,
     except that a message with no separate header is not printed twice
     (upstream shows "You are logged out" as the title and again as the body). -->
<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <#if messageHeader??>
            ${kcSanitize(msg("${messageHeader}"))?no_esc}
        <#else>
            ${message.summary}
        </#if>
    <#elseif section = "form">
    <div id="kc-info-message" class="akb-message">
        <#if messageHeader??>
            <p class="instruction akb-card__lead">${message.summary}<#if requiredActions??><#list requiredActions>: <b><#items as reqActionItem>${kcSanitize(msg("requiredAction.${reqActionItem}"))?no_esc}<#sep>, </#items></b></#list><#else></#if></p>
        <#elseif requiredActions??>
            <p class="instruction akb-card__lead"><#list requiredActions><b><#items as reqActionItem>${kcSanitize(msg("requiredAction.${reqActionItem}"))?no_esc}<#sep>, </#items></b></#list></p>
        </#if>
        <#if skipLink??>
        <#else>
            <#if pageRedirectUri?has_content>
                <p class="akb-form-actions"><a class="akb-button akb-button--primary akb-button--block akb-button--lg" href="${pageRedirectUri}">${msg("backToApplication")}</a></p>
            <#elseif actionUri?has_content>
                <p class="akb-form-actions"><a class="akb-button akb-button--primary akb-button--block akb-button--lg" href="${actionUri}">${msg("proceedWithAction")}</a></p>
            <#elseif (client.baseUrl)?has_content>
                <p class="akb-form-actions"><a class="akb-button akb-button--primary akb-button--block akb-button--lg" href="${client.baseUrl}">${msg("backToApplication")}</a></p>
            </#if>
        </#if>
    </div>
    </#if>
</@layout.registrationLayout>
