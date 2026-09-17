<template>
  <section class="hero-grid">
    <div class="hero-copy">
      <p class="section-kicker">EVALUATION-DRIVEN · FULLY PLUGGABLE</p>
      <h1>让每一次检索<br /><em>都有证据。</em></h1>
      <p class="hero-lead">面向企业知识场景的 Agentic RAG 平台。无需注册即可在隔离的演示工作区体验知识问答、文档处理、模型状态与全链路观测。</p>
      <div class="hero-actions">
        <RouterLink class="button button--primary" to="/chat">开始提问 <span>↗</span></RouterLink>
        <RouterLink class="button button--secondary" to="/workspace/documents">管理知识库</RouterLink>
      </div>
      <div class="trust-row">
        <span><i>✓</i> 工作区隔离</span><span><i>✓</i> 可验证引用</span><span><i>✓</i> 全链路观测</span>
      </div>
    </div>

    <div class="system-orbit" aria-label="可插拔 RAG 流程">
      <div class="orbit-card orbit-card--query"><small>QUERY</small><strong>企业年假政策有什么变化？</strong></div>
      <div class="orbit-core"><span>RAG</span><small>orchestrator</small></div>
      <div class="orbit-card orbit-card--answer"><small>GROUNDED ANSWER</small><strong>新增 2 天家庭照护假</strong><span>[1] 员工手册 · P12</span></div>
      <span class="orbit-node orbit-node--a">Dense</span><span class="orbit-node orbit-node--b">Sparse</span><span class="orbit-node orbit-node--c">Rerank</span>
    </div>

    <div class="metric-strip">
      <div><strong>2</strong><span>查询模式<br />Standard / Deep</span></div>
      <div><strong>6</strong><span>核心组件<br />支持灵活替换</span></div>
      <div><strong>100%</strong><span>引用可追溯<br />拒绝无依据回答</span></div>
    </div>
  </section>
</template>
