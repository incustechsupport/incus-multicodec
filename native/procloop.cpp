// procloop.dll: capture what every app plays, *before* the output device's effects, using Windows'
// process loopback (Windows 10 2004+): AUDIOCLIENT_PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
// with our own process as the target, i.e. "all audio except ours". Unlike endpoint loopback it is not
// shaped by the speaker's APOs (laptop bass cut, loudness, virtualizers), and needs no driver.
//
//   int  procloop_start(unsigned pid_to_exclude, int sample_rate, int channels, callback cb)
//   void procloop_stop()
//   callback(const float* interleaved, int frames)   called on the capture thread; silence = zeros
//
// Returns 0 on success or a failing HRESULT.

#include <windows.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <mmdeviceapi.h>

#include <atomic>
#include <thread>
#include <vector>

#pragma comment(lib, "mmdevapi.lib")
#pragma comment(lib, "ole32.lib")

typedef void(__cdecl* procloop_callback)(const float* data, int frames);

namespace {

class Completion : public IActivateAudioInterfaceCompletionHandler, public IAgileObject {
public:
    HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HRESULT result = E_FAIL;
    IAudioClient* client = nullptr;

    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG r = --refs_;
        if (!r) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** out) override {
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IActivateAudioInterfaceCompletionHandler)) {
            *out = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
        } else if (riid == __uuidof(IAgileObject)) {
            *out = static_cast<IAgileObject*>(this);
        } else {
            *out = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation* op) override {
        IUnknown* unknown = nullptr;
        HRESULT activate_hr = E_FAIL;
        result = op->GetActivateResult(&activate_hr, &unknown);
        if (SUCCEEDED(result)) result = activate_hr;
        if (SUCCEEDED(result)) result = unknown->QueryInterface(IID_PPV_ARGS(&client));
        if (unknown) unknown->Release();
        SetEvent(done);
        return S_OK;
    }
    ~Completion() { CloseHandle(done); }

private:
    std::atomic<ULONG> refs_{1};
};

std::thread g_thread;
std::atomic<bool> g_running{false};
IAudioClient* g_client = nullptr;
IAudioCaptureClient* g_capture = nullptr;
HANDLE g_event = nullptr;

void capture_loop(procloop_callback cb, int channels, int sample_rate) {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    std::vector<float> silence;
    LARGE_INTEGER freq, now, last_audio, silence_clock;
    QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&last_audio);
    silence_clock = last_audio;
    const double idle_before_silence = 0.1;  // s without packets before we call it "nothing playing"
    while (g_running) {
        if (WaitForSingleObject(g_event, 20) == WAIT_TIMEOUT) {
            // Process loopback delivers nothing while no app plays. Once it has been quiet for a moment,
            // emit silence at real-time pace (by the clock, not per timeout) so the encoder and the
            // Bluetooth stream keep running. Never while packets are flowing: that would add gaps.
            QueryPerformanceCounter(&now);
            double idle = double(now.QuadPart - last_audio.QuadPart) / freq.QuadPart;
            if (idle >= idle_before_silence) {
                double owed = double(now.QuadPart - silence_clock.QuadPart) / freq.QuadPart;
                int frames = static_cast<int>(owed * sample_rate);
                if (frames > 0) {
                    silence.assign(static_cast<size_t>(frames) * channels, 0.0f);
                    cb(silence.data(), frames);
                    silence_clock.QuadPart += static_cast<LONGLONG>(double(frames) / sample_rate * freq.QuadPart);
                }
            }
            continue;
        }
        QueryPerformanceCounter(&last_audio);
        silence_clock = last_audio;
        UINT32 packet = 0;
        while (g_running && SUCCEEDED(g_capture->GetNextPacketSize(&packet)) && packet) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            if (FAILED(g_capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr))) break;
            if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                silence.assign(static_cast<size_t>(frames) * channels, 0.0f);
                cb(silence.data(), static_cast<int>(frames));
            } else {
                cb(reinterpret_cast<const float*>(data), static_cast<int>(frames));
            }
            g_capture->ReleaseBuffer(frames);
        }
    }
    CoUninitialize();
}

void release_all() {
    if (g_client) g_client->Stop();
    if (g_capture) { g_capture->Release(); g_capture = nullptr; }
    if (g_client) { g_client->Release(); g_client = nullptr; }
    if (g_event) { CloseHandle(g_event); g_event = nullptr; }
}

}  // namespace

extern "C" __declspec(dllexport) void procloop_stop() {
    g_running = false;
    if (g_thread.joinable()) g_thread.join();
    release_all();
}

extern "C" __declspec(dllexport) int procloop_start(unsigned pid_to_exclude, int sample_rate, int channels,
                                                    procloop_callback cb) {
    procloop_stop();
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (hr == RPC_E_CHANGED_MODE) hr = S_OK;

    AUDIOCLIENT_ACTIVATION_PARAMS params = {};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = pid_to_exclude;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE;
    PROPVARIANT activate = {};
    activate.vt = VT_BLOB;
    activate.blob.cbSize = sizeof(params);
    activate.blob.pBlobData = reinterpret_cast<BYTE*>(&params);

    auto* completion = new Completion();
    IActivateAudioInterfaceAsyncOperation* op = nullptr;
    if (SUCCEEDED(hr)) {
        hr = ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient), &activate,
                                         completion, &op);
    }
    if (SUCCEEDED(hr)) {
        WaitForSingleObject(completion->done, 5000);
        hr = completion->result;
        g_client = completion->client;
    }
    if (op) op->Release();
    completion->Release();

    WAVEFORMATEX format = {};
    format.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
    format.nChannels = static_cast<WORD>(channels);
    format.nSamplesPerSec = sample_rate;
    format.wBitsPerSample = 32;
    format.nBlockAlign = static_cast<WORD>(channels * 4);
    format.nAvgBytesPerSec = sample_rate * format.nBlockAlign;
    if (SUCCEEDED(hr)) {
        hr = g_client->Initialize(AUDCLNT_SHAREMODE_SHARED,
                                  AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                                      AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                                  200000 /* 20 ms */, 0, &format, nullptr);
    }
    if (SUCCEEDED(hr)) {
        g_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        hr = g_client->SetEventHandle(g_event);
    }
    if (SUCCEEDED(hr)) hr = g_client->GetService(IID_PPV_ARGS(&g_capture));
    if (SUCCEEDED(hr)) hr = g_client->Start();
    if (FAILED(hr)) {
        release_all();
        return static_cast<int>(hr);
    }
    g_running = true;
    g_thread = std::thread(capture_loop, cb, channels, sample_rate);
    return 0;
}
