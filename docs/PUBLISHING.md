# Publishing feedback-hub to PyPI

## Step 1 - Create a PyPI account

Go to https://pypi.org/account/register/ and create an account for the BITS org (or use an existing one). Verify your email.

## Step 2 - Configure a Trusted Publisher (no token needed)

This is the modern approach - GitHub Actions authenticates directly with PyPI via OIDC, no API token to rotate.

1. Go to https://pypi.org/manage/account/publishing/
2. Under **Add a new pending publisher**, fill in:
   - **PyPI project name**: `feedback-hub`
   - **Owner**: `Community-Access`
   - **Repository**: `feedback-hub`
   - **Workflow name**: `publish.yml`
   - **Environment**: `pypi`
3. Click **Add**

## Step 3 - Create the `pypi` environment in GitHub

1. Go to https://github.com/Community-Access/feedback-hub/settings/environments
2. Click **New environment**, name it `pypi`
3. No secrets needed - the OIDC trust handles auth

## Step 4 - Create a GitHub release to trigger the publish

```
gh release create v1.0.0 --repo Community-Access/feedback-hub \
  --title "feedback-hub 1.0.0" \
  --notes "Initial public release."
```

This triggers `publish.yml`, which builds the package and uploads it to PyPI. The package will be live at https://pypi.org/project/feedback-hub/ within a minute or two.

## Step 5 - Fix ChapterForge CI

Once it's on PyPI, the existing `requirements.txt` entry (`feedback-hub>=1.0`) works as-is and CI will pass.
