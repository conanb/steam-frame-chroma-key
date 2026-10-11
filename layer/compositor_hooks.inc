// ---------------------------------------------------------------- compositor role
// Identify compositor shaders by hashing the SPIR-V files on disk, flag the
// pipelines that sample through a YCbCr (NV12) immutable sampler, and
// optionally substitute a patched SPIR-V for a named shader.
// (#included into mrchroma_layer.cpp)
#include <dirent.h>

static std::unordered_map<uint64_t, std::string> g_shaderNames;
static std::once_flag g_shaderNamesOnce;
static uint64_t fnv64(const void *p, size_t n) {
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < n; i++) h = (h ^ ((const uint8_t *)p)[i]) * 1099511628211ull;
    return h;
}
static std::vector<char> readFile(const std::string &path) {
    std::vector<char> v;
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return v;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (n > 0) {
        v.resize(n);
        if (fread(v.data(), 1, n, f) != (size_t)n) v.clear();
    }
    fclose(f);
    return v;
}
static void indexShaders() {
    const char *dirs[] = {"/opt/steamvr/resources/shaders/vulkan", "/opt/steamvr/drivers/cv/resources/shaders"};
    for (const char *dname : dirs) {
        DIR *d = opendir(dname);
        if (!d) continue;
        while (dirent *e = readdir(d)) {
            std::string n = e->d_name;
            if (n.size() < 5 || n.substr(n.size() - 4) != ".spv") continue;
            auto v = readFile(std::string(dname) + "/" + n);
            if (!v.empty()) g_shaderNames[fnv64(v.data(), v.size())] = n;
        }
        closedir(d);
    }
    LOG("indexed %zu shader files", g_shaderNames.size());
}

static VKAPI_ATTR VkResult VKAPI_CALL CreateShaderModule(VkDevice dev, const VkShaderModuleCreateInfo *ci,
                                                         const VkAllocationCallbacks *a, VkShaderModule *out) {
    DeviceData *d = getDev(key(dev));
    std::call_once(g_shaderNamesOnce, indexShaders);
    uint64_t h = fnv64(ci->pCode, ci->codeSize);
    auto it = g_shaderNames.find(h);
    std::string name = it != g_shaderNames.end() ? it->second : "";
    VkShaderModuleCreateInfo mci = *ci;
    std::vector<char> repl;
    std::string tag;
    if (!name.empty()) {
        loadConfig(false);
        Config c = cfg();
        for (auto &o : c.overrides) {
            if (o.first != name) continue;
            repl = readFile(o.second);
            if (repl.size() >= 20 && repl.size() % 4 == 0 && *(const uint32_t *)repl.data() == 0x07230203) {
                mci.pCode = (const uint32_t *)repl.data();
                mci.codeSize = repl.size();
                tag = " OVERRIDDEN by " + o.second;
            } else {
                LOG("override for %s: bad SPIR-V file %s, ignored", name.c_str(), o.second.c_str());
            }
        }
    }
    VkResult r = d->CreateShaderModule(dev, &mci, a, out);
    if (r != VK_SUCCESS && !tag.empty()) { // patched shader rejected: fall back to the original
        LOG("override of %s failed (%d), using original", name.c_str(), r);
        tag.clear();
        r = d->CreateShaderModule(dev, ci, a, out);
    }
    if (r != VK_SUCCESS) return r;
    char hs[24];
    snprintf(hs, sizeof hs, "%016llx", (unsigned long long)h);
    std::lock_guard<std::mutex> l(d->m);
    d->modName[*out] = name.empty() ? std::string("?") + hs : name;
    LOG("ShaderModule %p %s (%zu B)%s", (void *)*out, d->modName[*out].c_str(), ci->codeSize, tag.c_str());
    return r;
}

static VKAPI_ATTR VkResult VKAPI_CALL CreateSampler(VkDevice dev, const VkSamplerCreateInfo *ci,
                                                    const VkAllocationCallbacks *a, VkSampler *out) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->CreateSampler(dev, ci, a, out);
    if (r == VK_SUCCESS && findPNext(ci->pNext, VK_STRUCTURE_TYPE_SAMPLER_YCBCR_CONVERSION_INFO)) {
        std::lock_guard<std::mutex> l(d->m);
        d->ycbcrSamplers.insert(*out);
        LOG("YCbCr sampler %p", (void *)*out);
    }
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL CreateSamplerYcbcrConversion(VkDevice dev,
                                                                   const VkSamplerYcbcrConversionCreateInfo *ci,
                                                                   const VkAllocationCallbacks *a,
                                                                   VkSamplerYcbcrConversion *out) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->CreateSamplerYcbcrConversion(dev, ci, a, out);
    LOG("YCbCr conversion fmt=%d model=%d range=%d chroma=%d,%d filter=%d -> %d", ci->format, ci->ycbcrModel,
        ci->ycbcrRange, ci->xChromaOffset, ci->yChromaOffset, ci->chromaFilter, r);
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL CreateDescriptorSetLayout(VkDevice dev, const VkDescriptorSetLayoutCreateInfo *ci,
                                                                const VkAllocationCallbacks *a,
                                                                VkDescriptorSetLayout *out) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->CreateDescriptorSetLayout(dev, ci, a, out);
    if (r != VK_SUCCESS) return r;
    std::lock_guard<std::mutex> l(d->m);
    for (uint32_t i = 0; i < ci->bindingCount; i++) {
        auto &b = ci->pBindings[i];
        if (!b.pImmutableSamplers) continue;
        for (uint32_t j = 0; j < b.descriptorCount; j++)
            if (d->ycbcrSamplers.count(b.pImmutableSamplers[j])) {
                d->ycbcrDsl.insert(*out);
                LOG("YCbCr set layout %p binding %u", (void *)*out, b.binding);
                break;
            }
    }
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL CreatePipelineLayout(VkDevice dev, const VkPipelineLayoutCreateInfo *ci,
                                                           const VkAllocationCallbacks *a, VkPipelineLayout *out) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->CreatePipelineLayout(dev, ci, a, out);
    if (r != VK_SUCCESS) return r;
    std::lock_guard<std::mutex> l(d->m);
    for (uint32_t i = 0; i < ci->setLayoutCount; i++)
        if (d->ycbcrDsl.count(ci->pSetLayouts[i])) d->ycbcrPl.insert(*out);
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL CreateGraphicsPipelines(VkDevice dev, VkPipelineCache pc, uint32_t n,
                                                              const VkGraphicsPipelineCreateInfo *ci,
                                                              const VkAllocationCallbacks *a, VkPipeline *out) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->CreateGraphicsPipelines(dev, pc, n, ci, a, out);
    std::lock_guard<std::mutex> l(d->m);
    for (uint32_t i = 0; i < n; i++) {
        if (!out[i]) continue;
        std::string desc;
        for (uint32_t s = 0; s < ci[i].stageCount; s++) {
            auto it = d->modName.find(ci[i].pStages[s].module);
            desc += (s ? "+" : "") + (it != d->modName.end() ? it->second : std::string("?"));
        }
        if (d->ycbcrPl.count(ci[i].layout)) desc += " [YCBCR]";
        if (ci[i].pColorBlendState && ci[i].pColorBlendState->attachmentCount) {
            auto &b = ci[i].pColorBlendState->pAttachments[0];
            char bs[96];
            snprintf(bs, sizeof bs, " blend=%d src=%d dst=%d srcA=%d dstA=%d mask=%x", b.blendEnable,
                     b.srcColorBlendFactor, b.dstColorBlendFactor, b.srcAlphaBlendFactor, b.dstAlphaBlendFactor,
                     b.colorWriteMask);
            desc += bs;
        }
        d->pipeDesc[out[i]] = desc;
        LOG("GraphicsPipeline %p %s", (void *)out[i], desc.c_str());
    }
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL CreateComputePipelines(VkDevice dev, VkPipelineCache pc, uint32_t n,
                                                             const VkComputePipelineCreateInfo *ci,
                                                             const VkAllocationCallbacks *a, VkPipeline *out) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->CreateComputePipelines(dev, pc, n, ci, a, out);
    std::lock_guard<std::mutex> l(d->m);
    for (uint32_t i = 0; i < n; i++) {
        if (!out[i]) continue;
        auto it = d->modName.find(ci[i].stage.module);
        std::string desc = it != d->modName.end() ? it->second : "?";
        if (d->ycbcrPl.count(ci[i].layout)) desc += " [YCBCR]";
        d->pipeDesc[out[i]] = desc;
        LOG("ComputePipeline %p %s", (void *)out[i], desc.c_str());
    }
    return r;
}
static VKAPI_ATTR void VKAPI_CALL CmdBindPipeline(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipeline p) {
    DeviceData *d = getDev(key(cb));
    {
        std::lock_guard<std::mutex> l(d->m);
        d->binds[p]++;
        if (bp == VK_PIPELINE_BIND_POINT_GRAPHICS) d->cbPipe[cb] = p;
    }
    d->CmdBindPipeline(cb, bp, p);
}

// ---- camera-pass uniform probe.  tracked_camera_reprojection_*_ps outputs
// camera.rgb * tint.rgb * tint.a (tint = UBO set 0 binding 4, member at +304; a
// content-aware gradient at +416/+464 can replace tint.rgb).  We only record where
// that UBO lives and read it through the compositor's own host mapping at summary time.
static VKAPI_ATTR VkResult VKAPI_CALL BindBufferMemory(VkDevice dev, VkBuffer b, VkDeviceMemory mem, VkDeviceSize off) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->BindBufferMemory(dev, b, mem, off);
    if (r == VK_SUCCESS) { std::lock_guard<std::mutex> l(d->m); d->bufMem[b] = {mem, off}; }
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL BindBufferMemory2(VkDevice dev, uint32_t n, const VkBindBufferMemoryInfo *bi) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->BindBufferMemory2(dev, n, bi);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::mutex> l(d->m);
        for (uint32_t i = 0; i < n; i++) d->bufMem[bi[i].buffer] = {bi[i].memory, bi[i].memoryOffset};
    }
    return r;
}
static VKAPI_ATTR VkResult VKAPI_CALL MapMemory(VkDevice dev, VkDeviceMemory mem, VkDeviceSize off, VkDeviceSize size,
                                               VkMemoryMapFlags f, void **pp) {
    DeviceData *d = getDev(key(dev));
    VkResult r = d->MapMemory(dev, mem, off, size, f, pp);
    if (r == VK_SUCCESS) { std::lock_guard<std::mutex> l(d->m); d->mapped[mem] = {(char *)*pp, off}; }
    return r;
}
static VKAPI_ATTR void VKAPI_CALL UnmapMemory(VkDevice dev, VkDeviceMemory mem) {
    DeviceData *d = getDev(key(dev));
    {
        std::lock_guard<std::mutex> l(d->m);
        d->mapped.erase(mem);
    }
    d->UnmapMemory(dev, mem);
}
static VKAPI_ATTR void VKAPI_CALL UpdateDescriptorSets(VkDevice dev, uint32_t nw, const VkWriteDescriptorSet *w,
                                                       uint32_t nc, const VkCopyDescriptorSet *c) {
    DeviceData *d = getDev(key(dev));
    {
        std::lock_guard<std::mutex> l(d->m);
        for (uint32_t i = 0; i < nw; i++)
            if ((w[i].descriptorType == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER ||
                 w[i].descriptorType == VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC) && w[i].pBufferInfo)
                for (uint32_t j = 0; j < w[i].descriptorCount; j++)
                    d->setBinding[(uint64_t)w[i].dstSet ^ (w[i].dstBinding + j)] = {w[i].pBufferInfo[j].buffer,
                                                                                    w[i].pBufferInfo[j].offset};
    }
    d->UpdateDescriptorSets(dev, nw, w, nc, c);
}
static VKAPI_ATTR void VKAPI_CALL CmdBindDescriptorSets(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipelineLayout pl,
                                                        uint32_t first, uint32_t n, const VkDescriptorSet *sets,
                                                        uint32_t ndyn, const uint32_t *dyn) {
    DeviceData *d = getDev(key(cb));
    {
        std::lock_guard<std::mutex> l(d->m);
        auto it = d->cbPipe.find(cb);
        if (first == 0 && n > 0 && it != d->cbPipe.end() &&
            d->pipeDesc[it->second].find("tracked_camera_reprojection") != std::string::npos) {
            d->camSet = sets[0];
            d->camDynCount = ndyn;
            d->camDynOff = ndyn ? dyn[0] : 0;
        }
    }
    d->CmdBindDescriptorSets(cb, bp, pl, first, n, sets, ndyn, dyn);
}
static void cameraProbe(DeviceData *d) { // called with d->m held
    if (!d->camSet) { LOG("camera probe: no camera pass bound yet"); return; }
    auto sb = d->setBinding.find((uint64_t)d->camSet ^ 4);
    if (sb == d->setBinding.end()) { LOG("camera probe: set %p binding 4 not seen", (void *)d->camSet); return; }
    auto bm = d->bufMem.find(sb->second.first);
    if (bm == d->bufMem.end()) { LOG("camera probe: buffer memory unknown"); return; }
    auto mp = d->mapped.find(bm->second.first);
    if (mp == d->mapped.end()) { LOG("camera probe: buffer memory not host-mapped"); return; }
    VkDeviceSize off = bm->second.second + sb->second.second + d->camDynOff;
    if (off < mp->second.second) return;
    const float *u = (const float *)(mp->second.first + (off - mp->second.second));
    const float *t = u + 304 / 4, *g = u + 416 / 4, *c = u + 464 / 4;
    LOG("camera probe: tint=(%.3f %.3f %.3f) opacity=%.3f gradient=%.3f corners=(%.2f %.2f %.2f)(%.2f %.2f %.2f) dyn=%u",
        t[0], t[1], t[2], t[3], g[3], c[0], c[1], c[2], c[4], c[5], c[6], d->camDynCount);
}
static void compositorSummary(DeviceData *d) { // called with d->m held, every ~5 s
    std::vector<std::pair<uint64_t, VkPipeline>> v;
    for (auto &b : d->binds) v.emplace_back(b.second, b.first);
    std::sort(v.begin(), v.end(), [](const auto &x, const auto &y) { return x.first > y.first; });
    LOG("pipeline binds in last period (%zu distinct):", v.size());
    for (size_t i = 0; i < v.size() && i < 25; i++)
        LOG("  %6llu  %p %s", (unsigned long long)v[i].first, (void *)v[i].second, d->pipeDesc[v[i].second].c_str());
    d->binds.clear();
    cameraProbe(d);
}
