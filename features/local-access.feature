@implemented @python
Feature: Explicit token-free local deployment
  Token authentication remains the default. A localhost-only deployment may
  explicitly allow anonymous access without changing Copilot authentication.

  Scenario: Default deployment requires a browser token
    Given browser authentication is "default"
    When I request the session catalogue without a token
    Then the HTTP status is 401

  Scenario: Local deployment opens and creates chats without a token
    Given browser authentication is "local"
    When I create a chat without a token
    Then the HTTP status is 200
    And the created chat appears in the session catalogue
    And the browser configuration reports token authentication disabled

  Scenario: Explicitly invalid credentials are still rejected
    Given browser authentication is "local"
    When I request the session catalogue with an invalid token
    Then the HTTP status is 401

  Scenario: A foreign website cannot use anonymous local access
    Given browser authentication is "local"
    When a foreign website creates a chat without a token
    Then the HTTP status is 403
    And the session catalogue is empty

  Scenario: Cross-site reads cannot use anonymous local access
    Given browser authentication is "local"
    When a cross-site browser requests the session catalogue
    Then the HTTP status is 403

  Scenario: Restoring authentication preserves the existing token
    Given browser authentication is "local"
    When I restart with browser authentication required
    And I request the session catalogue without a token
    Then the HTTP status is 401
    And the original browser token still grants access
