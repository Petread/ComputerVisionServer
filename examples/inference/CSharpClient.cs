using System;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Threading.Tasks;

internal static class CSharpClient
{
    private static readonly Uri BaseUri = new(
        Environment.GetEnvironmentVariable("CV_PLATFORM_BASE_URL")
        ?? "https://127.0.0.1:8443/inference-rt/api/v1/"
    );

    public static async Task Main()
    {
        using var client = new HttpClient { BaseAddress = BaseUri, Timeout = TimeSpan.FromSeconds(5) };
        var token = Environment.GetEnvironmentVariable("CV_PLATFORM_INFERENCE_TOKEN");
        if (!string.IsNullOrWhiteSpace(token))
        {
            client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        }
        client.DefaultRequestHeaders.Add("X-Request-ID", "csharp-example-req-001");
        client.DefaultRequestHeaders.Add("X-Correlation-ID", "csharp-example-op-001");

        using var form = new MultipartFormDataContent();
        form.Add(new StringContent("quality-classifier"), "model_id");
        form.Add(new StringContent("2"), "top_k");

        await using var fileStream = File.OpenRead("photo.png");
        using var imageContent = new StreamContent(fileStream);
        imageContent.Headers.ContentType = new MediaTypeHeaderValue("image/png");
        form.Add(imageContent, "image", "photo.png");

        using var response = await client.PostAsync("infer/classify", form);
        response.EnsureSuccessStatusCode();

        var body = await response.Content.ReadAsStringAsync();
        Console.WriteLine(body);
    }
}
